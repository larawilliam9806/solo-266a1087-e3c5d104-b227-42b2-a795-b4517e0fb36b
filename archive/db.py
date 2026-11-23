"""SQLite schema and connection helpers for the film archive hand-in service."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no    TEXT    NOT NULL UNIQUE,
    film_no     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    row_count   INTEGER NOT NULL CHECK (row_count > 0)
);

-- One archive reel per original reel.  next_frame is the archive tail:
-- the first archive frame that has not been used yet.
CREATE TABLE IF NOT EXISTS reels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    orig_reel   TEXT    NOT NULL UNIQUE,
    next_frame  INTEGER NOT NULL DEFAULT 0 CHECK (next_frame >= 0)
);

-- Coverage index: an original frame interval and the archive interval that
-- holds a copy of it.  Intervals are half-open: [orig_start, orig_start+count)
-- and [arch_start, arch_start+count).  Rows never overlap per reel.
CREATE TABLE IF NOT EXISTS archive_segments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    reel_id          INTEGER NOT NULL REFERENCES reels(id),
    orig_start       INTEGER NOT NULL CHECK (orig_start >= 0),
    frame_count      INTEGER NOT NULL CHECK (frame_count > 0),
    arch_start       INTEGER NOT NULL CHECK (arch_start >= 0),
    created_order_id INTEGER REFERENCES orders(id),
    UNIQUE (reel_id, orig_start)
);
CREATE INDEX IF NOT EXISTS idx_archive_segments_reel_orig
    ON archive_segments (reel_id, orig_start);

-- Parsed rows of the submitted edit list.
CREATE TABLE IF NOT EXISTS shots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    row_index   INTEGER NOT NULL CHECK (row_index > 0),
    shot_no     TEXT    NOT NULL,
    orig_reel   TEXT    NOT NULL,
    orig_start  INTEGER NOT NULL CHECK (orig_start >= 0),
    frame_count INTEGER NOT NULL CHECK (frame_count > 0),
    UNIQUE (order_id, row_index)
);

-- What this order has to read/copy, one row per distinct original segment.
-- is_new = 1 when the order caused the copy, 0 when it reuses archive frames.
CREATE TABLE IF NOT EXISTS order_copies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    orig_reel   TEXT    NOT NULL,
    orig_start  INTEGER NOT NULL CHECK (orig_start >= 0),
    frame_count INTEGER NOT NULL CHECK (frame_count > 0),
    arch_start  INTEGER NOT NULL CHECK (arch_start >= 0),
    is_new      INTEGER NOT NULL CHECK (is_new IN (0, 1)),
    UNIQUE (order_id, orig_reel, orig_start)
);
CREATE INDEX IF NOT EXISTS idx_order_copies_order
    ON order_copies (order_id, orig_reel, orig_start);

-- Retrieval plan: for every shot row, the archive segments in the order in
-- which they must be read to rebuild that row's original frame sequence.
CREATE TABLE IF NOT EXISTS shot_segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    row_index   INTEGER NOT NULL CHECK (row_index > 0),
    shot_no     TEXT    NOT NULL,
    orig_reel   TEXT    NOT NULL,
    seg_index   INTEGER NOT NULL CHECK (seg_index >= 0),
    orig_start  INTEGER NOT NULL CHECK (orig_start >= 0),
    frame_count INTEGER NOT NULL CHECK (frame_count > 0),
    arch_start  INTEGER NOT NULL CHECK (arch_start >= 0),
    UNIQUE (order_id, row_index, seg_index)
);
CREATE INDEX IF NOT EXISTS idx_shot_segments_order
    ON shot_segments (order_id, row_index, seg_index);

-- Safety net: original frame intervals in one reel may never overlap.
CREATE TRIGGER IF NOT EXISTS trg_archive_orig_no_overlap
BEFORE INSERT ON archive_segments
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM archive_segments
    WHERE reel_id = NEW.reel_id
      AND NEW.orig_start < orig_start + frame_count
      AND orig_start < NEW.orig_start + NEW.frame_count
)
BEGIN
    SELECT RAISE(ABORT, 'archive segment overlaps an existing original range');
END;

-- Safety net: archive frame intervals in one reel may never overlap.
CREATE TRIGGER IF NOT EXISTS trg_archive_arch_no_overlap
BEFORE INSERT ON archive_segments
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM archive_segments
    WHERE reel_id = NEW.reel_id
      AND NEW.arch_start < arch_start + frame_count
      AND arch_start < NEW.arch_start + NEW.frame_count
)
BEGIN
    SELECT RAISE(ABORT, 'archive segment overlaps an existing archive range');
END;

-- Safety net: frames can only be written below the current reel tail.
CREATE TRIGGER IF NOT EXISTS trg_archive_within_tail
BEFORE INSERT ON archive_segments
FOR EACH ROW
WHEN NEW.arch_start + NEW.frame_count > (
    SELECT next_frame FROM reels WHERE id = NEW.reel_id
)
BEGIN
    SELECT RAISE(ABORT, 'archive segment exceeds the reel tail');
END;
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection configured for concurrent, explicit transactions."""
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> None:
    """Create the schema if needed.  Safe to call from every worker.

    Several gunicorn workers may boot at the same instant; the WAL switch and
    DDL briefly need the write lock, so retry while another process holds it.
    """
    path = Path(db_path)
    if str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    last_error: sqlite3.OperationalError | None = None
    for _ in range(50):
        conn = connect(path)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(SCHEMA)
            return
        except sqlite3.OperationalError as exc:  # pragma: no cover - timing
            last_error = exc
        finally:
            conn.close()
        time.sleep(0.05)
    raise last_error  # pragma: no cover - only on persistent failure
