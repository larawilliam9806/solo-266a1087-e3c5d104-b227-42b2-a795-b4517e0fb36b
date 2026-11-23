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
* Planning runs against a lock-free read snapshot, without blocking other
  submissions.  The plan is then reconciled with the current coverage index
  and written inside one short ``BEGIN IMMEDIATE`` transaction, so a large
  CSV never keeps other orders waiting.
* If frames of a planned copy were archived by another order in the meantime,
  the plan falls back to the actual archive location.  Frames still missing
  are appended, in CSV row order, at the current reel tail.
* An order, its copies and its per-shot retrieval plan become visible in one
  transaction.  Any failure rolls the whole submission back, leaving previous
  orders and reel tails untouched.
"""

from __future__ import annotations

import bisect
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .db import connect

MAX_IDENTIFIER_LEN = 200
MAX_ROWS = 200_000
MAX_FRAME = 2**62

# A save is first prepared against a lock-free snapshot, then applied in a
# short write transaction.  If another order commits in between, the
# preparation is repeated; after this many optimistic attempts we fall back
# to reconciling inside the write transaction so the order always progresses.
APPLY_ATTEMPTS = 4


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


def _load_reel(
    conn: sqlite3.Connection, orig_reel: str, create: bool = False
) -> _ReelState | None:
    """Read one reel's tail and coverage index.

    With ``create=True`` a missing reel is inserted with tail 0.  Planning
    uses ``create=False`` so it never writes.
    """
    row = conn.execute(
        "SELECT id, orig_reel, next_frame FROM reels WHERE orig_reel = ?",
        (orig_reel,),
    ).fetchone()
    if row is None:
        if not create:
            return None
        conn.execute(
            "INSERT OR IGNORE INTO reels (orig_reel, next_frame) VALUES (?, 0)",
            (orig_reel,),
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


def _coverage_events(state: _ReelState, start: int, end: int):
    """Walk ``[start, end)`` and describe existing coverage and gaps.

    Returns ordered ``("gap", s, e)`` and ``("cov", s, e, arch_start)``
    events that together cover the interval without overlaps.
    """
    events = []
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
    return events


def _plan_interval(state: _ReelState, start: int, end: int):
    """Split ``[start, end)`` into ordered, contiguous archive pieces.

    Returns ``(pieces, new_segments)``.  ``pieces`` is an ordered list of
    ``(orig_start, frame_count, arch_start, is_new)`` covering the whole
    interval without gaps; ``new_segments`` lists the freshly allocated
    ``(orig_start, frame_count, arch_start)`` ranges.  Existing coverage is
    reused in place, uncovered gaps are appended to the reel tail.
    """
    events = _coverage_events(state, start, end)

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


@dataclass
class _RowPlan:
    """Planned archive pieces for one CSV row (kept in CSV row order)."""

    row: EditRow
    pieces: list  # [(orig_start, frame_count, arch_start, is_new)]


@dataclass
class _ReelPlan:
    """Planning result for one original reel, valid against a snapshot."""

    orig_reel: str
    snapshot_tail: int
    snapshot_segments: tuple
    state: _ReelState | None = None
    rows: list = field(default_factory=list)
    referenced: list = field(default_factory=list)


def _empty_reel_state(orig_reel: str) -> _ReelState:
    return _ReelState(None, orig_reel, 0, [])


def _snapshot_reels(conn: sqlite3.Connection, orig_reels) -> dict[str, _ReelState]:
    """Read the reels touched by an order without taking the write lock."""
    snapshot = {}
    for orig_reel in orig_reels:
        snapshot[orig_reel] = (
            _load_reel(conn, orig_reel, create=False)
            or _empty_reel_state(orig_reel)
        )
    return snapshot


def _load_reel_delta(
    conn: sqlite3.Connection, plan: _ReelPlan, after_id: int
) -> _ReelState:
    """Read only what one reel gained since the plan's snapshot.

    An ``archive_segments`` row is never rewritten, so rows at or below the
    snapshot's highest id are immutable.  The commit-time re-check therefore
    reads only newer rows -- an index range scan on the primary key -- which
    keeps the write lock short even when a reel already has a huge coverage
    index.
    """
    row = conn.execute(
        "SELECT id, next_frame FROM reels WHERE orig_reel = ?",
        (plan.orig_reel,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO reels (orig_reel, next_frame) VALUES (?, 0)",
            (plan.orig_reel,),
        )
        row = conn.execute(
            "SELECT id, next_frame FROM reels WHERE orig_reel = ?",
            (plan.orig_reel,),
        ).fetchone()
    fresh = conn.execute(
        "SELECT orig_start, frame_count, arch_start "
        "FROM archive_segments WHERE reel_id = ? AND id > ? "
        "ORDER BY orig_start",
        (row["id"], after_id),
    ).fetchall()
    return _ReelState(
        row["id"],
        plan.orig_reel,
        row["next_frame"],
        [(s["orig_start"], s["frame_count"], s["arch_start"]) for s in fresh],
    )


def _merged_state(plan: _ReelPlan, delta: _ReelState) -> _ReelState:
    """Full current reel state: immutable snapshot plus its read delta."""
    tail = max(delta.tail, plan.snapshot_tail)
    return _ReelState(
        delta.reel_id,
        plan.orig_reel,
        tail,
        list(plan.snapshot_segments) + list(delta.segments),
    )


def _delta_touches(delta: _ReelState, intervals) -> bool:
    """True when frames appended since the snapshot overlap ``intervals``.

    Runs inside the short write transaction, so it binary-searches the
    reference intervals instead of comparing every pair.
    """
    referenced = _merge_intervals(intervals)
    if not referenced or not delta.segments:
        return False
    starts = [start for start, _end in referenced]
    for seg_start, seg_count, _arch in delta.segments:
        seg_end = seg_start + seg_count
        # First referenced interval that could reach past seg_start.
        idx = bisect.bisect_right(starts, seg_start) - 1
        if idx >= 0 and referenced[idx][1] > seg_start:
            return True
        # Any referenced interval starting inside [seg_start, seg_end).
        nxt = idx + 1
        if nxt < len(referenced) and referenced[nxt][0] < seg_end:
            return True
    return False


def _plan_rows(rows, snapshot: dict[str, _ReelState]) -> list[_ReelPlan]:
    """Compute the whole order against a snapshot.  Pure in-memory work.

    Rows are walked in CSV order, so uncovered frames are allocated to each
    reel tail in the order they appear in the edit list.
    """
    plans: dict[str, _ReelPlan] = {}
    for row in rows:
        plan = plans.get(row.orig_reel)
        if plan is None:
            snap = snapshot[row.orig_reel]
            plan = _ReelPlan(
                row.orig_reel,
                snap.tail,
                tuple(snap.segments),
                _ReelState(
                    snap.reel_id, snap.orig_reel, snap.tail, list(snap.segments)
                ),
            )
            plans[row.orig_reel] = plan
        pieces, _new = _plan_interval(plan.state, row.orig_start, row.orig_end)
        plan.rows.append(_RowPlan(row, pieces))
        plan.referenced.append((row.orig_start, row.orig_end))
    return list(plans.values())


def _planned_new_segments(plan: _ReelPlan):
    """Freshly allocated ``(orig_start, count, arch_start)`` of one reel."""
    return [
        (start, count, arch)
        for row_plan in plan.rows
        for start, count, arch, is_new in row_plan.pieces
        if is_new
    ]


def _shift_allocations(plan: _ReelPlan, fresh: _ReelState) -> None:
    """Move this order's own allocations to a newer reel tail.

    Called when other orders appended frames this order does not use.  Every
    piece at or above the snapshot tail was allocated by this order (existing
    coverage always lies below the snapshot tail), so shifting by the tail
    delta keeps the pieces contiguous and in CSV row order.
    """
    delta = fresh.tail - plan.snapshot_tail
    plan.state.reel_id = fresh.reel_id
    if delta:
        for row_plan in plan.rows:
            pieces = []
            for start, count, arch, is_new in row_plan.pieces:
                if arch >= plan.snapshot_tail:
                    arch += delta
                pieces.append((start, count, arch, is_new))
            row_plan.pieces = pieces
    new_segments = _planned_new_segments(plan)
    plan.state.tail = fresh.tail + sum(count for _s, count, _a in new_segments)
    plan.state.segments = sorted(list(fresh.segments) + new_segments)
    plan.state.starts = [s[0] for s in plan.state.segments]


def _replan(plan: _ReelPlan, fresh: _ReelState) -> None:
    """Re-plan one reel against the actual current coverage.

    Another order archived frames of this order in the meantime.  Frames that
    are now covered keep their real landing points; still-missing frames are
    appended to the current tail in CSV row order.
    """
    plan.state = _ReelState(
        fresh.reel_id, plan.orig_reel, fresh.tail, list(fresh.segments)
    )
    for row_plan in plan.rows:
        pieces, _new = _plan_interval(
            plan.state, row_plan.row.orig_start, row_plan.row.orig_end
        )
        row_plan.pieces = pieces


def _render_plan_rows(plans):
    """Turn in-memory plans into binding parameters for each table.

    Depends only on plan state, so it runs outside the write transaction.  The
    manifest keeps one row per distinct covered range per reel, coalescing
    overlapping CSV rows.
    """
    shots = []
    shot_segments = []
    copies = []
    archives = []
    for plan in plans:
        for row_plan in plan.rows:
            row = row_plan.row
            shots.append(
                (
                    row.row_index,
                    row.shot_no,
                    row.orig_reel,
                    row.orig_start,
                    row.frame_count,
                )
            )
            for seg_index, (start, count, arch, _is_new) in enumerate(
                row_plan.pieces
            ):
                shot_segments.append(
                    (
                        row.row_index,
                        row.shot_no,
                        row.orig_reel,
                        seg_index,
                        start,
                        count,
                        arch,
                    )
                )
        new_segments = _planned_new_segments(plan)
        archives.extend(
            (plan.orig_reel, start, count, arch)
            for start, count, arch in new_segments
        )
        for start, count, arch, is_new in _coverage_pieces(
            plan.state, plan.referenced, set(new_segments)
        ):
            copies.append(
                (plan.orig_reel, start, count, arch, 1 if is_new else 0)
            )
    return shots, shot_segments, copies, archives


def _insert_many(conn, sql, params) -> None:
    if params:
        conn.executemany(sql, params)


class _SnapshotStale(Exception):
    """The reels changed since the plan was made; re-plan outside the lock."""


def _write_order(conn, order_no, film_no, row_count, shots, shot_segments,
                 copies, archives, plans, reel_ids) -> None:
    """Insert an already-rendered order.  Runs inside ``BEGIN IMMEDIATE``."""
    cur = conn.execute(
        "INSERT INTO orders (order_no, film_no, created_at, row_count) "
        "VALUES (?, ?, ?, ?)",
        (order_no, film_no, _now(), row_count),
    )
    order_id = cur.lastrowid

    # Raise every reel tail before inserting archive segments so the tail
    # safety trigger accepts all new ranges.
    for plan in plans:
        if _planned_new_segments(plan):
            conn.execute(
                "UPDATE reels SET next_frame = ? WHERE id = ?",
                (plan.state.tail, reel_ids[plan.orig_reel]),
            )
    _insert_many(
        conn,
        "INSERT INTO archive_segments "
        "(reel_id, orig_start, frame_count, arch_start, created_order_id) "
        "VALUES (?, ?, ?, ?, ?)",
        [(reel_ids[orig_reel], start, count, arch, order_id)
         for orig_reel, start, count, arch in archives],
    )
    _insert_many(
        conn,
        "INSERT INTO shots "
        "(order_id, row_index, shot_no, orig_reel, orig_start, frame_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(order_id, *params) for params in shots],
    )
    _insert_many(
        conn,
        "INSERT INTO shot_segments "
        "(order_id, row_index, shot_no, orig_reel, seg_index, orig_start, "
        " frame_count, arch_start) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(order_id, *params) for params in shot_segments],
    )
    _insert_many(
        conn,
        "INSERT INTO order_copies "
        "(order_id, orig_reel, orig_start, frame_count, arch_start, is_new) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(order_id, *params) for params in copies],
    )


def submit_order(
    db_path: str | Path,
    order_no: str,
    film_no: str,
    rows,
) -> dict:
    """Plan and store one hand-in order atomically, then return its report.

    Planning, manifest assembly and the per-shot retrieval split all run
    against a read snapshot outside any write transaction.  The completed
    order is committed in one short ``BEGIN IMMEDIATE`` transaction that only
    validates the referenced coverage and inserts rows, so other submissions
    keep completing -- including appends to the same reels -- while a large
    CSV is being computed.

    Concurrent changes are reconciled at commit time: if frames this order
    needs were archived by another order, their real landing points are
    adopted; frames still missing are appended to the latest tail in CSV row
    order.  If only unrelated frames were appended, the planned segments stay
    in place and freshly copied pieces shift to the new tail.  Any failure
    rolls the whole order back; committed orders and their reel tails remain.
    """
    order_no = clean_identifier(order_no, "单号")
    film_no = clean_identifier(film_no, "成片号")
    rows = validate_rows(rows)

    orig_reels = []
    seen_reels = set()
    for row in rows:
        if row.orig_reel not in seen_reels:
            seen_reels.add(row.orig_reel)
            orig_reels.append(row.orig_reel)

    conn = connect(db_path)
    try:
        for attempt in range(APPLY_ATTEMPTS):
            # Phase 1: duplicate check and a consistent read snapshot.  In WAL
            # mode this read transaction does not block other writers.
            conn.execute("BEGIN")
            try:
                exists = conn.execute(
                    "SELECT 1 FROM orders WHERE order_no = ?", (order_no,)
                ).fetchone()
                if exists:
                    raise DuplicateOrderError(
                        f"单号 {order_no} 已存在，请勿重复交档"
                    )
                snapshot = _snapshot_reels(conn, orig_reels)
                segment_watermark = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM archive_segments"
                ).fetchone()[0]
            finally:
                conn.execute("COMMIT")

            # Phase 2: plan the whole order in memory, holding no lock.
            plans = _plan_rows(rows, snapshot)
            prepared = _render_plan_rows(plans)

            # Phase 3: short write transaction -- re-check the reels, adjust
            # the prepared plan if needed, and insert everything.
            conn.execute("BEGIN IMMEDIATE")
            try:
                exists = conn.execute(
                    "SELECT 1 FROM orders WHERE order_no = ?", (order_no,)
                ).fetchone()
                if exists:
                    raise DuplicateOrderError(
                        f"单号 {order_no} 已存在，请勿重复交档"
                    )

                deltas = {}
                reel_ids = {}
                stale = set()
                for plan in plans:
                    delta = _load_reel_delta(conn, plan, segment_watermark)
                    deltas[plan.orig_reel] = delta
                    reel_ids[plan.orig_reel] = delta.reel_id
                    if _delta_touches(delta, plan.referenced):
                        stale.add(plan.orig_reel)
                if stale and attempt < APPLY_ATTEMPTS - 1:
                    # Some frames this order needs were archived meanwhile.
                    # Re-plan outside the lock rather than while holding it.
                    raise _SnapshotStale

                # Adjust the prepared plan cheaply: adopt actual landing
                # points for concurrently archived frames, and move only this
                # order's own copies to the current tail.  On the final
                # attempt this also covers the stale case.
                changed = False
                for plan in plans:
                    delta = deltas[plan.orig_reel]
                    if plan.orig_reel in stale:
                        _replan(plan, _merged_state(plan, delta))
                        changed = True
                    elif delta.tail != plan.snapshot_tail:
                        _shift_allocations(
                            plan, _merged_state(plan, delta)
                        )
                        changed = True
                    else:
                        plan.state.reel_id = delta.reel_id

                if changed:
                    prepared = _render_plan_rows(plans)
                shots, shot_segments, copies, archives = prepared
                _write_order(
                    conn,
                    order_no,
                    film_no,
                    len(rows),
                    shots,
                    shot_segments,
                    copies,
                    archives,
                    plans,
                    reel_ids,
                )
                conn.execute("COMMIT")
                break
            except _SnapshotStale:
                # Another order committed frames this order needs.  Back off
                # outside the lock, then take a fresh snapshot and re-plan.
                conn.execute("ROLLBACK")
                time.sleep(0.02 * (2**attempt))
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
