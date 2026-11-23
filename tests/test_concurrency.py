import threading
import time

import pytest

from archive.core import SubmissionError, get_order, reel_state, submit_order
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


def _run_concurrently(db, orders, results):
    """Run submissions in lock-step.  ``results`` is a list filled in order."""
    results.extend([None] * len(orders))
    barrier = threading.Barrier(len(orders))

    def worker(index, order_no, rows):
        barrier.wait()
        try:
            report = submit_order(db, order_no, "FILM", rows)
            results[index] = ("ok", report)
        except Exception as exc:  # noqa: BLE001 - collected for assertions
            results[index] = ("error", exc)

    threads = [
        threading.Thread(target=worker, args=(i, o[0], o[1]))
        for i, o in enumerate(orders)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)


def test_concurrent_orders_referencing_same_frames_copy_once(db):
    # Three orders all need the same original frames 0..100.
    results = []
    _run_concurrently(
        db,
        [(f"O{i}", [row(1, "S01", "R001", 0, 100)]) for i in range(1, 4)],
        results,
    )
    assert all(v[0] == "ok" for v in results), results
    state = reel_state(db, "R001")
    assert state["next_frame"] == 100
    assert len(state["segments"]) == 1
    # Exactly one order created the copy, the others reused it.
    new_flags = [
        copy["is_new"]
        for _, report in results
        for copy in report["copies"]
    ]
    assert new_flags.count(True) == 1
    assert new_flags.count(False) == 2


def test_concurrent_overlapping_orders_never_double_allocate(db):
    # Each order needs 0..100 plus 50 extra frames of its own.
    results = []
    orders = [
        (f"O{i}", [row(1, "S01", "R001", 0, 100 + i * 10)])
        for i in range(1, 5)
    ]
    _run_concurrently(db, orders, results)
    assert all(v[0] == "ok" for v in results), results

    state = reel_state(db, "R001")
    # Union of original frames is 0..130; archive must be exactly that long.
    assert state["next_frame"] == 140
    segs = sorted(state["segments"], key=lambda s: s["orig_start"])
    cursor_orig = 0
    cursor_arch = 0
    for seg in segs:
        assert seg["orig_start"] == cursor_orig
        assert seg["arch_start"] == cursor_arch
        cursor_orig = seg["orig_end"]
        cursor_arch = seg["arch_end"]
    assert cursor_orig == 140 and cursor_arch == 140

    # No order may claim to have created frames another order also created.
    total_new = sum(
        copy["frame_count"]
        for _, report in results
        for copy in report["copies"]
        if copy["is_new"]
    )
    assert total_new == 140


def test_concurrent_duplicate_order_number_only_one_wins(db):
    results = []
    _run_concurrently(
        db,
        [("SAME", [row(1, "S01", "R001", 0, 10)]) for _ in range(4)],
        results,
    )
    statuses = [v[0] for v in results]
    assert statuses.count("ok") == 1
    assert statuses.count("error") == 3
    assert all(isinstance(v[1], SubmissionError) for v in results if v[0] == "error")
    assert reel_state(db, "R001")["next_frame"] == 10
    assert get_order(db, "SAME") is not None


def test_large_interval_concurrency_stays_fast(db):
    span = 10_000_000
    results = []
    start = time.monotonic()
    _run_concurrently(
        db,
        [
            (f"O{i}", [row(1, "S01", "R001", i * 1000, span)])
            for i in range(1, 5)
        ],
        results,
    )
    elapsed = time.monotonic() - start
    assert all(v[0] == "ok" for v in results), results
    assert elapsed < 20
    state = reel_state(db, "R001")
    assert state["next_frame"] == span + 3000
