from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_map (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    yandex_uid          TEXT,
    yandex_href         TEXT,
    yandex_etag         TEXT,
    google_event_id     TEXT,
    google_etag         TEXT,
    content_hash        TEXT NOT NULL,
    source              TEXT NOT NULL,
    last_synced_at      TEXT NOT NULL,
    is_recurring        BOOLEAN DEFAULT FALSE,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(yandex_uid),
    UNIQUE(google_event_id)
);

CREATE INDEX IF NOT EXISTS idx_event_map_yandex_uid ON event_map(yandex_uid);
CREATE INDEX IF NOT EXISTS idx_event_map_google_id ON event_map(google_event_id);

CREATE TABLE IF NOT EXISTS sync_state (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    provider            TEXT NOT NULL UNIQUE,
    sync_token          TEXT,
    calendar_ctag       TEXT,
    last_sync_at        TEXT,
    last_full_sync_at   TEXT,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    direction           TEXT NOT NULL,
    events_created      INTEGER DEFAULT 0,
    events_updated      INTEGER DEFAULT 0,
    events_deleted      INTEGER DEFAULT 0,
    conflicts_resolved  INTEGER DEFAULT 0,
    errors              TEXT,
    status              TEXT NOT NULL DEFAULT 'running'
);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    """Create or open SQLite database and ensure schema exists."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
