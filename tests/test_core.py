import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from archive.core import SubmissionError, get_order, reel_state, submit_order  # noqa: E402
from archive.db import init_db  # noqa: E402

FILM = "FILM-A"


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


def submit(db, order_no, rows, film=FILM):
    return submit_order(db, order_no, film, rows)


def test_first_order_assigns_archive_from_zero(db):
    report = submit(db, "O1", [row(1, "S01", "R001", 100, 50), row(2, "S02", "R001", 200, 30)])
    assert [(c["orig_start"], c["orig_end"], c["arch_start"], c["arch_end"], c["is_new"])
            for c in report["copies"]] == [
        (100, 150, 0, 50, True),
        (200, 230, 50, 80, True),
    ]
    assert reel_state(db, "R001")["next_frame"] == 80


def test_reused_frames_keep_archive_numbers_and_new_frames_append(db):
    submit(db, "O1", [row(1, "S01", "R001", 100, 50)])
    report = submit(db, "O2", [row(1, "S01", "R001", 120, 60)])
    # 120..150 already archived at 20..50; 150..180 is new at tail 50..80.
    assert [(c["orig_start"], c["orig_end"], c["arch_start"], c["arch_end"], c["is_new"])
            for c in report["copies"]] == [
        (120, 150, 20, 50, False),
        (150, 180, 50, 80, True),
    ]
    assert [s["arch_start"] for s in report["shots"][0]["segments"]] == [20, 50]
    assert reel_state(db, "R001")["next_frame"] == 80


def test_partial_gap_in_middle_allocates_contiguously_at_tail(db):
    submit(db, "O1", [row(1, "S01", "R001", 0, 10), row(2, "S02", "R001", 20, 10)])
    report = submit(db, "O2", [row(1, "S01", "R001", 0, 30)])
    segs = report["shots"][0]["segments"]
    assert [(s["orig_start"], s["orig_end"], s["arch_start"]) for s in segs] == [
        (0, 10, 0),
        (10, 20, 20),   # new gap appended at tail 20
        (20, 30, 10),
    ]
    assert reel_state(db, "R001")["next_frame"] == 30


def test_same_frame_referenced_twice_in_one_order_copies_once(db):
    report = submit(
        db,
        "O1",
        [row(1, "S01", "R001", 100, 40), row(2, "S02", "R001", 110, 20)],
    )
    assert [(c["orig_start"], c["orig_end"], c["arch_start"], c["is_new"]) for c in report["copies"]] == [
        (100, 140, 0, True)
    ]
    # Both shots point at the single copy.
    assert [s["arch_start"] for s in report["shots"][0]["segments"]] == [0]
    assert [s["arch_start"] for s in report["shots"][1]["segments"]] == [10]
    assert reel_state(db, "R001")["next_frame"] == 40


def test_later_row_reusing_earlier_row_copies_marks_reuse_not_new(db):
    # Row 1 copies 0..100 at tail 0.  Row 2 needs 50..150: 50..100 is already
    # covered by this same order, so it must be reuse, not a second copy.
    report = submit(
        db,
        "O1",
        [row(1, "S01", "R001", 0, 100), row(2, "S02", "R001", 50, 100)],
    )
    assert [(c["orig_start"], c["orig_end"], c["arch_start"], c["is_new"]) for c in report["copies"]] == [
        (0, 150, 0, True)
    ]
    segs = report["shots"][1]["segments"]
    assert [(s["orig_start"], s["orig_end"], s["arch_start"]) for s in segs] == [
        (50, 100, 50),
        (100, 150, 100),
    ]
    assert reel_state(db, "R001")["next_frame"] == 150


def test_reuse_from_previous_order_is_flagged_not_new(db):
    submit(db, "O1", [row(1, "S01", "R001", 0, 100)])
    report = submit(db, "O2", [row(1, "S01", "R001", 0, 50), row(2, "S02", "R001", 25, 25)])
    assert [(c["orig_start"], c["orig_end"], c["arch_start"], c["is_new"]) for c in report["copies"]] == [
        (0, 50, 0, False)
    ]


def test_multi_reel_reports_split_reads(db):
    report = submit(
        db,
        "O1",
        [row(1, "S01", "R001", 5, 10), row(2, "S02", "R002", 0, 5)],
    )
    assert {c["orig_reel"] for c in report["copies"]} == {"R001", "R002"}
    assert reel_state(db, "R001")["next_frame"] == 10
    assert reel_state(db, "R002")["next_frame"] == 5


def test_retrieval_plan_rebuilds_original_frame_order(db):
    submit(db, "O1", [row(1, "S01", "R001", 0, 5), row(2, "S02", "R001", 10, 5)])
    submit(db, "O2", [row(1, "S03", "R001", 20, 5)])
    report = submit(db, "O3", [row(1, "S01", "R001", 0, 30)])
    segs = report["shots"][0]["segments"]
    # Concatenating the archive segments in order must yield 0..30.
    cursor = 0
    for seg in segs:
        assert seg["orig_start"] == cursor
        assert seg["frame_count"] > 0
        cursor = seg["orig_end"]
    assert cursor == 30


def test_failure_leaves_previous_orders_and_tails_untouched(db):
    submit(db, "O1", [row(1, "S01", "R001", 0, 10)])
    before = reel_state(db, "R001")
    with pytest.raises(SubmissionError):
        submit(db, "O2", [row(1, "S01", "R001", 10, 10), row(2, "S02", "R001", -1, 5)])
    assert reel_state(db, "R001") == before
    assert get_order(db, "O2") is None
    assert get_order(db, "O1") is not None


def test_unexpected_failure_mid_transaction_rolls_everything_back(db, monkeypatch):
    """A crash after some rows are written must not leak partial state."""
    import archive.core as core

    submit(db, "O1", [row(1, "S01", "R001", 0, 10)])
    before_reel = reel_state(db, "R001")
    before_order = get_order(db, "O1")

    original = core._coverage_pieces
    calls = {"n": 0}

    def boom(state, intervals, new_set):
        calls["n"] += 1
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(core, "_coverage_pieces", boom)
    with pytest.raises(RuntimeError):
        submit(db, "O2", [row(1, "S01", "R001", 10, 10)])
    assert calls["n"] == 1
    monkeypatch.setattr(core, "_coverage_pieces", original)

    assert reel_state(db, "R001") == before_reel
    assert get_order(db, "O1") == before_order
    assert get_order(db, "O2") is None

    # The connection is usable again: a fresh order succeeds cleanly.
    report = submit(db, "O3", [row(1, "S01", "R001", 10, 10)])
    assert report["copies"][0]["arch_start"] == 10


def test_duplicate_order_number_rejected_without_side_effects(db):
    submit(db, "O1", [row(1, "S01", "R001", 0, 10)])
    before = reel_state(db, "R001")
    with pytest.raises(SubmissionError):
        submit(db, "O1", [row(1, "S01", "R001", 100, 10)])
    assert reel_state(db, "R001") == before


def test_huge_frame_counts_are_interval_math_not_frame_loops(db):
    span = 50_000_000
    report = submit(db, "O1", [row(1, "S01", "R001", 0, span), row(2, "S02", "R001", 1, span - 1)])
    assert report["copies"][0]["frame_count"] == span
    assert reel_state(db, "R001")["next_frame"] == span


def test_overlapping_original_ranges_inside_one_order_do_not_double_copy(db):
    report = submit(
        db,
        "O1",
        [
            row(1, "S01", "R001", 0, 100),
            row(2, "S02", "R001", 50, 100),
            row(3, "S03", "R001", 120, 10),
        ],
    )
    assert reel_state(db, "R001")["next_frame"] == 150
    covered = sorted((c["orig_start"], c["orig_end"]) for c in report["copies"])
    assert covered == [(0, 150)]


def test_three_order_chain_reuses_every_existing_frame(db):
    submit(db, "O1", [row(1, "S01", "R001", 0, 100)])
    submit(db, "O2", [row(1, "S01", "R001", 100, 100)])
    report = submit(db, "O3", [row(1, "S01", "R001", 0, 200)])
    assert all(not c["is_new"] for c in report["copies"])
    assert reel_state(db, "R001")["next_frame"] == 200
    assert [(s["arch_start"], s["arch_end"]) for s in report["shots"][0]["segments"]] == [
        (0, 100),
        (100, 200),
    ]


def test_empty_and_invalid_rows_rejected(db):
    for rows in ([], [row(1, "S01", "R001", 0, 0)]):
        with pytest.raises(SubmissionError):
            submit(db, "OX", rows)


def test_schema_upgrade_is_idempotent_and_preserves_orders(db):
    """Old databases keep working: re-init adds indexes, keeps rows/tails."""
    from archive.db import SCHEMA_VERSION, connect, init_db

    submit(db, "O1", [row(1, "S01", "R001", 0, 10)])
    before = reel_state(db, "R001")
    conn = connect(db)
    conn.execute("PRAGMA user_version = 0")  # pretend it is an older DB
    conn.close()

    init_db(db)
    init_db(db)

    conn = connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    indexes = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    conn.close()
    assert "idx_archive_segments_reel_arch" in indexes
    assert reel_state(db, "R001") == before
    assert get_order(db, "O1") is not None
    # Upgraded database still accepts new orders on the same reel.
    report = submit(db, "O2", [row(1, "S01", "R001", 5, 10)])
    assert report["copies"][-1]["is_new"] is True
