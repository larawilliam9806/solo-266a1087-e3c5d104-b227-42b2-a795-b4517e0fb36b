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
CREATE INDEX IF NOT EXISTS idx_archive_segments_reel_arch
    ON archive_segments (reel_id, arch_start);

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

-- Interval checks use the index: with non-overlapping existing segments, a
-- new half-open interval [s, e) overlaps iff its nearest predecessor reaches
-- past s or its nearest successor starts before e.  Looking only at those two
-- rows keeps bulk inserts linear instead of scanning the whole reel per row.
DROP TRIGGER IF EXISTS trg_archive_orig_no_overlap;
CREATE TRIGGER trg_archive_orig_no_overlap
BEFORE INSERT ON archive_segments
FOR EACH ROW
WHEN (
    SELECT orig_start + frame_count FROM archive_segments
    WHERE reel_id = NEW.reel_id AND orig_start <= NEW.orig_start
    ORDER BY orig_start DESC LIMIT 1
) > NEW.orig_start
OR (
    SELECT 1 FROM archive_segments
    WHERE reel_id = NEW.reel_id
      AND orig_start >= NEW.orig_start
      AND orig_start < NEW.orig_start + NEW.frame_count
    LIMIT 1
) IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'archive segment overlaps an existing original range');
END;

-- Safety net: archive frame intervals in one reel may never overlap.
DROP TRIGGER IF EXISTS trg_archive_arch_no_overlap;
CREATE TRIGGER trg_archive_arch_no_overlap
BEFORE INSERT ON archive_segments
FOR EACH ROW
WHEN (
    SELECT arch_start + frame_count FROM archive_segments
    WHERE reel_id = NEW.reel_id AND arch_start <= NEW.arch_start
    ORDER BY arch_start DESC LIMIT 1
) > NEW.arch_start
OR (
    SELECT 1 FROM archive_segments
    WHERE reel_id = NEW.reel_id
      AND arch_start >= NEW.arch_start
      AND arch_start < NEW.arch_start + NEW.frame_count
    LIMIT 1
) IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'archive segment overlaps an existing archive range');
END;

-- Safety net: frames can only be written below the current reel tail.
DROP TRIGGER IF EXISTS trg_archive_within_tail;
CREATE TRIGGER trg_archive_within_tail
BEFORE INSERT ON archive_segments
FOR EACH ROW
WHEN NEW.arch_start + NEW.frame_count > (
    SELECT next_frame FROM reels WHERE id = NEW.reel_id
)
BEGIN
    SELECT RAISE(ABORT, 'archive segment exceeds the reel tail');
END;
"""


SCHEMA_VERSION = 2


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection configured for concurrent, explicit transactions."""
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL persists in the file, but synchronous is per-connection and would
    # otherwise fall back to FULL on every request.  NORMAL is durable across
    # application crashes in WAL mode and keeps commits short.
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _split_statements(script: str) -> list[str]:
    """Split a SQL script into complete statements, respecting trigger bodies.

    ``executescript`` would implicitly commit an open transaction, so the
    migration executes individual statements instead.  ``sqlite3
    .complete_statement`` knows that ``BEGIN ... END;`` trigger bodies are a
    single statement.
    """
    statements = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    if buffer.strip():
        statements.append(buffer)
    return statements


def init_db(db_path: str | Path) -> None:
    """Create or migrate the schema.  Safe to call from every worker.

    The DDL runs only when the on-disk schema is older than
    ``SCHEMA_VERSION``; a fresh or already-migrated database does no DDL, so
    workers booting together do not repeatedly drop and recreate triggers.
    The version check and DDL share one immediate transaction, so concurrent
    boots are serialised and see the upgraded version afterwards.  Existing
    data, order numbers and reel tails are left untouched.
    """
    path = Path(db_path)
    if str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    statements = _split_statements(SCHEMA)
    last_error: sqlite3.OperationalError | None = None
    for _ in range(50):
        conn = connect(path)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            if _user_version(conn) >= SCHEMA_VERSION:
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                if _user_version(conn) < SCHEMA_VERSION:
                    for statement in statements:
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return
        except sqlite3.OperationalError as exc:  # pragma: no cover - timing
            last_error = exc
        finally:
            conn.close()
        time.sleep(0.05)
    raise last_error  # pragma: no cover - only on persistent failure
