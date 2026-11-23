"""Core archive planning logic.

Everything works on half-open frame intervals ``[start, start + count)``.
Frame counts may be arbitrarily large; no code here iterates over individual
frames.

Invariants
----------
* Every original reel maps to exactly one archive reel whose frame numbering
  starts at 0.
* ``reels.next_frame`` is the archive reel tail: the first unused archive
  frame.  New material is always appended there, contiguously.
* ``archive_segments`` is a non-overlapping coverage index from original frame
  intervals to archive frame intervals.  Ranges already present are reused
  exactly where they are; only uncovered gaps are copied.
* An order, its copies and its per-shot retrieval plan become visible in one
  transaction.  Any failure rolls the whole submission back, leaving previous
  orders and reel tails untouched.
"""

from __future__ import annotations

import bisect
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .db import connect

MAX_IDENTIFIER_LEN = 200
MAX_ROWS = 200_000
MAX_FRAME = 2**62


class SubmissionError(Exception):
    """A rejected submission.  ``messages`` is safe to show to the user."""

    def __init__(self, messages):
        if isinstance(messages, str):
            messages = [messages]
        self.messages = list(messages)
        super().__init__("; ".join(self.messages))


class DuplicateOrderError(SubmissionError):
    """The order number is already taken."""


@dataclass(frozen=True)
class EditRow:
    row_index: int
    shot_no: str
    orig_reel: str
    orig_start: int
    frame_count: int

    @property
    def orig_end(self) -> int:
        return self.orig_start + self.frame_count


class _ReelState:
    """In-transaction view of one archive reel and its coverage index."""

    __slots__ = ("reel_id", "orig_reel", "tail", "segments", "starts")

    def __init__(self, reel_id, orig_reel, tail, segments):
        self.reel_id = reel_id
        self.orig_reel = orig_reel
        self.tail = tail
        self.segments = sorted(segments)  # (orig_start, count, arch_start)
        self.starts = [s[0] for s in self.segments]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_identifier(value, label: str) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        raise SubmissionError(f"{label}不能为空")
    if len(text) > MAX_IDENTIFIER_LEN:
        raise SubmissionError(f"{label}过长（最多 {MAX_IDENTIFIER_LEN} 个字符）")
    if any(ch in text for ch in "\r\n\x00"):
        raise SubmissionError(f"{label}含有非法字符")
    return text


def validate_rows(rows) -> list[EditRow]:
    if not isinstance(rows, list) or not rows:
        raise SubmissionError("剪辑清单为空，至少需要一行镜头记录")
    if len(rows) > MAX_ROWS:
        raise SubmissionError(f"剪辑清单行数过多（最多 {MAX_ROWS} 行）")
    cleaned: list[EditRow] = []
    problems: list[str] = []
    for row in rows:
        try:
            if isinstance(row, EditRow):
                cleaned.append(row)
                continue
            shot_no = clean_identifier(row["shot_no"], f"第 {row['row_index']} 行镜头号")
            orig_reel = clean_identifier(
                row["orig_reel"], f"第 {row['row_index']} 行原卷号"
            )
            orig_start = int(row["orig_start"])
            frame_count = int(row["frame_count"])
            if orig_start < 0:
                raise SubmissionError(f"第 {row['row_index']} 行起帧不能为负数")
            if frame_count <= 0:
                raise SubmissionError(f"第 {row['row_index']} 行帧数必须大于 0")
            if orig_start + frame_count > MAX_FRAME:
                raise SubmissionError(f"第 {row['row_index']} 行帧段超出允许范围")
            cleaned.append(
                EditRow(
                    row_index=int(row["row_index"]),
                    shot_no=shot_no,
                    orig_reel=orig_reel,
                    orig_start=orig_start,
                    frame_count=frame_count,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"第 {row.get('row_index', '?')} 行格式不正确: {exc}")
        except SubmissionError as exc:
            problems.extend(exc.messages)
    if problems:
        raise SubmissionError(problems)
    return cleaned


def _load_reel(conn: sqlite3.Connection, orig_reel: str) -> _ReelState:
    row = conn.execute(
        "SELECT id, orig_reel, next_frame FROM reels WHERE orig_reel = ?",
        (orig_reel,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO reels (orig_reel, next_frame) VALUES (?, 0)", (orig_reel,)
        )
        row = conn.execute(
            "SELECT id, orig_reel, next_frame FROM reels WHERE orig_reel = ?",
            (orig_reel,),
        ).fetchone()
    segments = conn.execute(
        "SELECT orig_start, frame_count, arch_start "
        "FROM archive_segments WHERE reel_id = ? ORDER BY orig_start",
        (row["id"],),
    ).fetchall()
    return _ReelState(
        row["id"],
        row["orig_reel"],
        row["next_frame"],
        [(s["orig_start"], s["frame_count"], s["arch_start"]) for s in segments],
    )


def _plan_interval(state: _ReelState, start: int, end: int):
    """Split ``[start, end)`` into ordered, contiguous archive pieces.

    Returns ``(pieces, new_segments)``.  ``pieces`` is an ordered list of
    ``(orig_start, frame_count, arch_start, is_new)`` covering the whole
    interval without gaps; ``new_segments`` lists the freshly allocated
    ``(orig_start, frame_count, arch_start)`` ranges.  Existing coverage is
    reused in place, uncovered gaps are appended to the reel tail.
    """
    events = []  # ("gap", s, e) | ("cov", s, e, arch_start)
    cursor = start
    idx = bisect.bisect_left(state.starts, start)
    if idx > 0:
        prev = state.segments[idx - 1]
        if prev[0] + prev[1] > start:
            idx -= 1
    while idx < len(state.segments):
        seg_start, seg_count, seg_arch = state.segments[idx]
        if seg_start >= end:
            break
        seg_end = seg_start + seg_count
        if seg_end > cursor:
            if seg_start > cursor:
                events.append(("gap", cursor, min(seg_start, end)))
            overlap_start = max(seg_start, start)
            overlap_end = min(seg_end, end)
            if overlap_end > overlap_start:
                events.append(
                    (
                        "cov",
                        overlap_start,
                        overlap_end,
                        seg_arch + (overlap_start - seg_start),
                    )
                )
            cursor = max(cursor, overlap_end)
        idx += 1
    if cursor < end:
        events.append(("gap", cursor, end))

    pieces = []
    new_segments = []
    tail = state.tail
    for event in events:
        if event[0] == "gap":
            gap_start, gap_end = event[1], event[2]
            count = gap_end - gap_start
            pieces.append((gap_start, count, tail, True))
            new_segments.append((gap_start, count, tail))
            bisect.insort(state.segments, (gap_start, count, tail))
            bisect.insort(state.starts, gap_start)
            tail += count
        else:
            pieces.append((event[1], event[2] - event[1], event[3], False))
    state.tail = tail
    return pieces, new_segments


def _merge_intervals(intervals):
    """Union of half-open ``(start, end)`` intervals, sorted and merged."""
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _coverage_pieces(state, intervals, new_set):
    """Split referenced intervals along the coverage index.

    Returns ``[(orig_start, count, arch_start, is_new)]``, one entry per
    coverage segment piece, canonicalised by the union of ``intervals``.
    """
    pieces = []
    idx = 0
    for iv_start, iv_end in _merge_intervals(intervals):
        while idx < len(state.segments) and (
            state.segments[idx][0] + state.segments[idx][1] <= iv_start
        ):
            idx += 1
        j = idx
        while j < len(state.segments):
            seg_start, seg_count, seg_arch = state.segments[j]
            seg_end = seg_start + seg_count
            if seg_start >= iv_end:
                break
            lo = max(seg_start, iv_start)
            hi = min(seg_end, iv_end)
            if hi > lo:
                pieces.append(
                    (
                        lo,
                        hi - lo,
                        seg_arch + (lo - seg_start),
                        (seg_start, seg_count, seg_arch) in new_set,
                    )
                )
            j += 1
    # Coalesce neighbours that stay contiguous in both frame spaces.
    merged = []
    for start, count, arch, is_new in pieces:
        if merged:
            p_start, p_count, p_arch, p_new = merged[-1]
            if (
                p_new == is_new
                and p_start + p_count == start
                and p_arch + p_count == arch
            ):
                merged[-1] = (p_start, p_count + count, p_arch, p_new)
                continue
        merged.append((start, count, arch, is_new))
    return merged


def submit_order(
    db_path: str | Path,
    order_no: str,
    film_no: str,
    rows,
) -> dict:
    """Plan and store one hand-in order atomically, then return its report."""
    order_no = clean_identifier(order_no, "单号")
    film_no = clean_identifier(film_no, "成片号")
    rows = validate_rows(rows)

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            exists = conn.execute(
                "SELECT 1 FROM orders WHERE order_no = ?", (order_no,)
            ).fetchone()
            if exists:
                raise DuplicateOrderError(f"单号 {order_no} 已存在，请勿重复交档")
            cur = conn.execute(
                "INSERT INTO orders (order_no, film_no, created_at, row_count) "
                "VALUES (?, ?, ?, ?)",
                (order_no, film_no, _now(), len(rows)),
            )
            order_id = cur.lastrowid

            reels: dict[str, _ReelState] = {}
            referenced: dict[str, list[tuple[int, int]]] = {}
            created: dict[str, list[tuple[int, int, int]]] = {}
            for row in rows:
                state = reels.get(row.orig_reel)
                if state is None:
                    state = _load_reel(conn, row.orig_reel)
                    reels[row.orig_reel] = state

                pieces, new_segments = _plan_interval(
                    state, row.orig_start, row.orig_end
                )

                # Raise the reel tail first so the safety triggers, which
                # compare against reels.next_frame, accept the new segments.
                if new_segments:
                    conn.execute(
                        "UPDATE reels SET next_frame = ? WHERE id = ?",
                        (state.tail, state.reel_id),
                    )
                    for seg_start, seg_count, seg_arch in new_segments:
                        conn.execute(
                            "INSERT INTO archive_segments "
                            "(reel_id, orig_start, frame_count, arch_start, "
                            " created_order_id) VALUES (?, ?, ?, ?, ?)",
                            (
                                state.reel_id,
                                seg_start,
                                seg_count,
                                seg_arch,
                                order_id,
                            ),
                        )

                referenced.setdefault(row.orig_reel, []).append(
                    (row.orig_start, row.orig_end)
                )
                created.setdefault(row.orig_reel, []).extend(new_segments)

                conn.execute(
                    "INSERT INTO shots "
                    "(order_id, row_index, shot_no, orig_reel, orig_start, "
                    " frame_count) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        order_id,
                        row.row_index,
                        row.shot_no,
                        row.orig_reel,
                        row.orig_start,
                        row.frame_count,
                    ),
                )

                for seg_index, (piece_start, piece_count, piece_arch, _) in enumerate(
                    pieces
                ):
                    conn.execute(
                        "INSERT INTO shot_segments "
                        "(order_id, row_index, shot_no, orig_reel, seg_index, "
                        " orig_start, frame_count, arch_start) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            order_id,
                            row.row_index,
                            row.shot_no,
                            row.orig_reel,
                            seg_index,
                            piece_start,
                            piece_count,
                            piece_arch,
                        ),
                    )

            # One manifest row per distinct covered range this order touches.
            for orig_reel, intervals in referenced.items():
                state = reels[orig_reel]
                new_set = set(created.get(orig_reel, ()))
                for start, count, arch, is_new in _coverage_pieces(
                    state, intervals, new_set
                ):
                    conn.execute(
                        "INSERT INTO order_copies "
                        "(order_id, orig_reel, orig_start, frame_count, "
                        " arch_start, is_new) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            order_id,
                            orig_reel,
                            start,
                            count,
                            arch,
                            1 if is_new else 0,
                        ),
                    )

            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    report = get_order(db_path, order_no)
    if report is None:  # pragma: no cover - committed rows must be visible
        raise RuntimeError("order vanished after commit")
    return report


def _order_report(conn: sqlite3.Connection, order) -> dict:
    order_id = order["id"]
    copies = conn.execute(
        "SELECT orig_reel, orig_start, frame_count, arch_start, is_new "
        "FROM order_copies WHERE order_id = ? "
        "ORDER BY orig_reel, orig_start",
        (order_id,),
    ).fetchall()
    shots = conn.execute(
        "SELECT row_index, shot_no, orig_reel, orig_start, frame_count "
        "FROM shots WHERE order_id = ? ORDER BY row_index",
        (order_id,),
    ).fetchall()
    segment_rows = conn.execute(
        "SELECT row_index, seg_index, orig_start, frame_count, arch_start "
        "FROM shot_segments WHERE order_id = ? "
        "ORDER BY row_index, seg_index",
        (order_id,),
    ).fetchall()

    segments_by_row: dict[int, list[dict]] = {}
    for seg in segment_rows:
        segments_by_row.setdefault(seg["row_index"], []).append(
            {
                "seg_index": seg["seg_index"],
                "orig_start": seg["orig_start"],
                "orig_end": seg["orig_start"] + seg["frame_count"],
                "frame_count": seg["frame_count"],
                "arch_start": seg["arch_start"],
                "arch_end": seg["arch_start"] + seg["frame_count"],
            }
        )

    reel_rows = conn.execute(
        "SELECT r.orig_reel, r.next_frame FROM reels r "
        "WHERE r.orig_reel IN ("
        "  SELECT orig_reel FROM order_copies WHERE order_id = ?"
        ") ORDER BY r.orig_reel",
        (order_id,),
    ).fetchall()

    return {
        "order_no": order["order_no"],
        "film_no": order["film_no"],
        "created_at": order["created_at"],
        "row_count": order["row_count"],
        "copies": [
            {
                "orig_reel": c["orig_reel"],
                "orig_start": c["orig_start"],
                "orig_end": c["orig_start"] + c["frame_count"],
                "frame_count": c["frame_count"],
                "arch_start": c["arch_start"],
                "arch_end": c["arch_start"] + c["frame_count"],
                "is_new": bool(c["is_new"]),
            }
            for c in copies
        ],
        "shots": [
            {
                "row_index": s["row_index"],
                "shot_no": s["shot_no"],
                "orig_reel": s["orig_reel"],
                "orig_start": s["orig_start"],
                "orig_end": s["orig_start"] + s["frame_count"],
                "frame_count": s["frame_count"],
                "segments": segments_by_row.get(s["row_index"], []),
            }
            for s in shots
        ],
        "touched_reels": [
            {"orig_reel": r["orig_reel"], "next_frame": r["next_frame"]}
            for r in reel_rows
        ],
    }


def get_order(db_path: str | Path, order_no: str) -> dict | None:
    conn = connect(db_path)
    try:
        order = conn.execute(
            "SELECT id, order_no, film_no, created_at, row_count "
            "FROM orders WHERE order_no = ?",
            (order_no,),
        ).fetchone()
        if order is None:
            return None
        return _order_report(conn, order)
    finally:
        conn.close()


def list_orders(db_path: str | Path, limit: int = 50) -> list[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT order_no, film_no, created_at, row_count FROM orders "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def reel_state(db_path: str | Path, orig_reel: str) -> dict | None:
    """Return the archive tail and coverage index of one original reel."""
    conn = connect(db_path)
    try:
        reel = conn.execute(
            "SELECT id, orig_reel, next_frame FROM reels WHERE orig_reel = ?",
            (orig_reel,),
        ).fetchone()
        if reel is None:
            return None
        segments = conn.execute(
            "SELECT s.orig_start, s.frame_count, s.arch_start, "
            "       s.created_order_id, o.order_no AS created_order_no "
            "FROM archive_segments s "
            "LEFT JOIN orders o ON o.id = s.created_order_id "
            "WHERE s.reel_id = ? ORDER BY s.orig_start",
            (reel["id"],),
        ).fetchall()
        return {
            "orig_reel": reel["orig_reel"],
            "next_frame": reel["next_frame"],
            "segments": [
                {
                    "orig_start": s["orig_start"],
                    "orig_end": s["orig_start"] + s["frame_count"],
                    "frame_count": s["frame_count"],
                    "arch_start": s["arch_start"],
                    "arch_end": s["arch_start"] + s["frame_count"],
                    "created_order_id": s["created_order_id"],
                    "created_order_no": s["created_order_no"],
                }
                for s in segments
            ],
        }
    finally:
        conn.close()
