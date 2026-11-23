"""Concurrency behaviour of the snapshot-plan / short-commit pipeline.

These tests pause an order *between* taking its read snapshot and committing,
so the race against another order is deterministic rather than timing-based.
"""

import threading
import time

import pytest

import archive.core as core
from archive.core import get_order, reel_state, submit_order
from archive.db import init_db


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "archive.sqlite3"
    init_db(path)
    return path


def row(index, shot, reel, start, count):
    return {
        "row_index": index,
        "shot_no": shot,
        "orig_reel": reel,
        "orig_start": start,
        "frame_count": count,
    }


class _Pause:
    """Hold one order after planning, then let another order run."""

    def __init__(self, monkeypatch, marker):
        self.marker = marker
        self.entered = threading.Event()
        self.release = threading.Event()
        original = core._plan_rows

        def hooked(rows, snapshot):
            plans = original(rows, snapshot)
            if any(r.shot_no == marker for r in rows):
                self.entered.set()
                if not self.release.wait(10):
                    raise RuntimeError("test never released the paused order")
            return plans

        monkeypatch.setattr(core, "_plan_rows", hooked)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release.set()
        return False


def _run(target, *args):
    box = {}

    def wrap():
        try:
            box["result"] = target(*args)
        except BaseException as exc:  # noqa: BLE001 - re-raised by caller
            box["error"] = exc

    thread = threading.Thread(target=wrap)
    thread.start()
    return thread, box


def test_snapshot_planning_does_not_block_other_orders(db, monkeypatch):
    """While one order computes its plan, a same-reel order still registers."""
    submit_order(db, "SEED", "F", [row(1, "S", "R001", 0, 10)])

    with _Pause(monkeypatch, "BIG") as pause:
        thread, box = _run(
            submit_order,
            db,
            "BIG",
            "F",
            [row(i + 1, "BIG", "R001", 1_000 + i * 10, 5) for i in range(40)],
        )
        assert pause.entered.wait(10), "big order never reached planning"

        start = time.monotonic()
        other = submit_order(db, "OTHER", "F", [row(1, "S", "R001", 0, 5)])
        elapsed = time.monotonic() - start
        pause.release.set()
        thread.join(15)

    assert "error" not in box, box.get("error")
    assert elapsed < 2.0
    assert other["copies"][0]["is_new"] is False
    assert reel_state(db, "R001")["next_frame"] == 10 + 40 * 5


def test_frames_archived_meanwhile_keep_their_actual_landing(db, monkeypatch):
    submit_order(db, "SEED", "F", [row(1, "S", "R001", 0, 10)])

    with _Pause(monkeypatch, "SLOW") as pause:
        thread, box = _run(
            submit_order, db, "SLOW", "F", [row(1, "SLOW", "R001", 5, 20)]
        )
        assert pause.entered.wait(10)
        # Archives 15..25 at the tail (10..20) while SLOW is planning 5..25.
        submit_order(db, "OTHER", "F", [row(1, "S", "R001", 15, 10)])
        pause.release.set()
        thread.join(15)

    assert "error" not in box, box.get("error")
    report = get_order(db, "SLOW")
    assert [
        (c["orig_start"], c["orig_end"], c["arch_start"], c["is_new"])
        for c in report["copies"]
    ] == [
        (5, 10, 5, False),
        (10, 15, 20, True),
        (15, 25, 10, False),
    ]
    # Retrieval order still rebuilds 5..25 in original frame order.
    assert [
        (s["orig_start"], s["orig_end"], s["arch_start"])
        for s in report["shots"][0]["segments"]
    ] == [
        (5, 10, 5),
        (10, 15, 20),
        (15, 25, 10),
    ]
    assert reel_state(db, "R001")["next_frame"] == 25


def test_new_copies_slide_when_only_unrelated_frames_were_appended(
    db, monkeypatch
):
    submit_order(db, "SEED", "F", [row(1, "S", "R001", 0, 10)])

    with _Pause(monkeypatch, "SLOW") as pause:
        thread, box = _run(
            submit_order, db, "SLOW", "F", [row(1, "SLOW", "R001", 20, 10)]
        )
        assert pause.entered.wait(10)
        # Unrelated frames land at the tail (10..20); SLOW planned at 10..20.
        submit_order(db, "OTHER", "F", [row(1, "S", "R001", 500, 10)])
        pause.release.set()
        thread.join(15)

    assert "error" not in box, box.get("error")
    report = get_order(db, "SLOW")
    # The already-divided shot segment is kept; only its landing point moves.
    assert [
        (s["orig_start"], s["orig_end"], s["arch_start"])
        for s in report["shots"][0]["segments"]
    ] == [(20, 30, 20)]
    assert [
        (c["orig_start"], c["arch_start"], c["is_new"])
        for c in report["copies"]
    ] == [(20, 20, True)]
    assert reel_state(db, "R001")["next_frame"] == 30


def test_stale_reconciliation_also_works_on_final_attempt(
    db, monkeypatch
):
    """Even without a retry budget the in-lock fallback stays correct."""
    monkeypatch.setattr(core, "APPLY_ATTEMPTS", 1)
    submit_order(db, "SEED", "F", [row(1, "S", "R001", 0, 10)])

    with _Pause(monkeypatch, "SLOW") as pause:
        thread, box = _run(
            submit_order, db, "SLOW", "F", [row(1, "SLOW", "R001", 5, 20)]
        )
        assert pause.entered.wait(10)
        submit_order(db, "OTHER", "F", [row(1, "S", "R001", 15, 10)])
        pause.release.set()
        thread.join(15)

    assert "error" not in box, box.get("error")
    report = get_order(db, "SLOW")
    assert [
        (s["orig_start"], s["arch_start"])
        for s in report["shots"][0]["segments"]
    ] == [(5, 5), (10, 20), (15, 10)]
    assert reel_state(db, "R001")["next_frame"] == 25


def test_failed_save_keeps_committed_orders_and_tails(db, monkeypatch):
    submit_order(db, "SEED", "F", [row(1, "S", "R001", 0, 10)])
    before = reel_state(db, "R001")

    import archive.core as core

    original = core._write_order

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(core, "_write_order", boom)
    with pytest.raises(RuntimeError):
        submit_order(db, "NEW", "F", [row(1, "S", "R001", 10, 10)])
    monkeypatch.setattr(core, "_write_order", original)

    assert reel_state(db, "R001") == before
    assert get_order(db, "NEW") is None
    assert get_order(db, "SEED") is not None
    # The service recovers and the next order still appends at the same tail.
    report = submit_order(db, "NEXT", "F", [row(1, "S", "R001", 10, 10)])
    assert report["copies"][0]["arch_start"] == 10


def test_new_reel_created_concurrently_does_not_double_allocate(db, monkeypatch):
    """Both orders start from an empty reel; one creates it, the other reuses."""
    with _Pause(monkeypatch, "B") as pause:
        thread, box = _run(
            submit_order, db, "B", "F", [row(1, "B", "R9", 0, 10)]
        )
        assert pause.entered.wait(10)
        submit_order(db, "A", "F", [row(1, "A", "R9", 0, 10)])
        pause.release.set()
        thread.join(15)

    assert "error" not in box, box.get("error")
    state = reel_state(db, "R9")
    assert state["next_frame"] == 10
    assert len(state["segments"]) == 1
    assert get_order(db, "B")["copies"][0]["is_new"] is False


def test_missing_frames_are_appended_in_csv_row_order(db):
    """Unarchived frames follow the CSV row order at the latest tail."""
    submit_order(db, "SEED", "F", [row(1, "S", "R001", 0, 10)])
    report = submit_order(
        db,
        "O1",
        "F",
        [
            row(1, "A", "R001", 100, 5),
            row(2, "B", "R001", 50, 5),
            row(3, "C", "R001", 200, 5),
        ],
    )
    assert [
        (c["orig_start"], c["arch_start"]) for c in report["copies"]
    ] == [(50, 15), (100, 10), (200, 20)]
    assert reel_state(db, "R001")["next_frame"] == 25


def test_row_order_applies_to_frames_missing_after_replan(db, monkeypatch):
    """A frame still missing after a concurrent archive lands by row order."""
    submit_order(db, "SEED", "F", [row(1, "S", "R001", 0, 10)])
    with _Pause(monkeypatch, "SLOW") as pause:
        thread, box = _run(
            submit_order,
            db,
            "SLOW",
            "F",
            [
                row(1, "SLOW", "R001", 100, 5),
                row(2, "SLOW", "R001", 50, 5),
            ],
        )
        assert pause.entered.wait(10)
        # Archive 100..105 meanwhile; at commit 50..55 is the only missing one.
        submit_order(db, "OTHER", "F", [row(1, "S", "R001", 100, 5)])
        pause.release.set()
        thread.join(15)

    assert "error" not in box, box.get("error")
    report = get_order(db, "SLOW")
    # 100..105 was archived at 10..15 by OTHER; 50..55 is still missing and is
    # appended at the current tail 15..20.
    assert [
        (c["orig_start"], c["arch_start"], c["is_new"])
        for c in report["copies"]
    ] == [(50, 15, True), (100, 10, False)]
    assert reel_state(db, "R001")["next_frame"] == 20


def test_same_order_later_row_reuse_still_shifts_with_tail(db, monkeypatch):
    """A later row reusing this order's own new frame also moves with the tail.

    Row 1 copies 0..10 at the planned tail; row 2 reuses part of it and adds
    more.  If another order appends unrelated frames meanwhile, both the new
    piece and the reused piece must move together.
    """
    submit_order(db, "SEED", "F", [row(1, "S", "R001", 0, 10)])
    with _Pause(monkeypatch, "SLOW") as pause:
        thread, box = _run(
            submit_order,
            db,
            "SLOW",
            "F",
            [
                row(1, "SLOW", "R001", 100, 10),
                row(2, "SLOW", "R001", 105, 10),
            ],
        )
        assert pause.entered.wait(10)
        submit_order(db, "OTHER", "F", [row(1, "S", "R001", 500, 5)])
        pause.release.set()
        thread.join(15)

    assert "error" not in box, box.get("error")
    report = get_order(db, "SLOW")
    # Tail after SEED+OTHER is 15; row 1 needs 100..110 -> 15..25; row 2 needs
    # 105..115, reusing 105..110 at 20..25 and appending 110..115 at 25..30.
    assert [
        (s["orig_start"], s["orig_end"], s["arch_start"])
        for s in report["shots"][0]["segments"]
    ] == [(100, 110, 15)]
    assert [
        (s["orig_start"], s["orig_end"], s["arch_start"])
        for s in report["shots"][1]["segments"]
    ] == [(105, 110, 20), (110, 115, 25)]
    assert reel_state(db, "R001")["next_frame"] == 30
