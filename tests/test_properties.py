"""Randomised invariant checks: interval bookkeeping and retrieval plans."""

import random

import pytest

from archive.core import reel_state, submit_order
from archive.db import init_db


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "archive.sqlite3"
    init_db(path)
    return path


def merged(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _fully_covered_and_mapped(segments, orig_start, orig_end, arch_start):
    """True when the range is covered piecewise and maps to ``arch_start``."""
    cursor = orig_start
    while cursor < orig_end:
        for s in segments:
            if s["orig_start"] <= cursor < s["orig_end"]:
                expected_arch = s["arch_start"] + (cursor - s["orig_start"])
                assert expected_arch == arch_start + (cursor - orig_start)
                cursor = min(s["orig_end"], orig_end)
                break
        else:
            return False
    return True


def test_random_orders_preserve_all_invariants(db):
    rng = random.Random(20261123)
    orders = []
    for n in range(60):
        row_count = rng.randint(1, 5)
        rows = []
        for i in range(row_count):
            start = rng.randrange(0, 400)
            count = rng.randint(1, 60)
            rows.append(
                {
                    "row_index": i + 1,
                    "shot_no": f"S{i + 1}",
                    "orig_reel": rng.choice(["R1", "R2"]),
                    "orig_start": start,
                    "frame_count": count,
                }
            )
        order_no = f"O{n}"
        report = submit_order(db, order_no, "FILM", rows)
        orders.append((order_no, rows, report))

    referenced = {"R1": [], "R2": []}
    for order_no, rows, report in orders:
        for r in rows:
            referenced[r["orig_reel"]].append((r["orig_start"], r["orig_start"] + r["frame_count"]))

    for reel in ("R1", "R2"):
        state = reel_state(db, reel)
        expected = merged(referenced[reel])
        actual = [(s["orig_start"], s["orig_end"]) for s in state["segments"]]
        # Coverage index is a non-overlapping partition of the referenced set.
        # Adjacent pieces from different orders legitimately stay separate.
        assert merged(actual) == expected
        assert all(e > s for s, e in actual)
        for (s1, e1), (s2, _) in zip(actual, actual[1:]):
            assert e1 <= s2
        total = sum(e - s for s, e in expected)
        assert state["next_frame"] == total
        # Archive ranges tile 0..tail without gaps or overlaps.
        arch = sorted((s["arch_start"], s["arch_end"]) for s in state["segments"])
        cursor = 0
        for s, e in arch:
            assert s == cursor
            cursor = e
        assert cursor == total

    for order_no, rows, report in orders:
        for shot in report["shots"]:
            cursor = shot["orig_start"]
            for seg in shot["segments"]:
                assert seg["orig_start"] == cursor
                assert seg["frame_count"] > 0
                cursor = seg["orig_end"]
                # The archive piece must be backed by the coverage index.
                state = reel_state(db, shot["orig_reel"])
                assert _fully_covered_and_mapped(
                    state["segments"],
                    seg["orig_start"],
                    seg["orig_end"],
                    seg["arch_start"],
                )
            assert cursor == shot["orig_end"]

        # Every manifest copy is genuinely covered by the final index.
        for copy in report["copies"]:
            state = reel_state(db, copy["orig_reel"])
            assert _fully_covered_and_mapped(
                state["segments"],
                copy["orig_start"],
                copy["orig_end"],
                copy["arch_start"],
            )
