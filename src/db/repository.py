from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class EventMapping:
    id: int | None
    yandex_uid: str | None
    yandex_href: str | None
    yandex_etag: str | None
    google_event_id: str | None
    google_etag: str | None
    content_hash: str
    source: str
    last_synced_at: str
    is_recurring: bool = False


@dataclass
class SyncState:
    provider: str
    sync_token: str | None = None
    calendar_ctag: str | None = None
    last_sync_at: str | None = None
    last_full_sync_at: str | None = None


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # === Event Mapping ===

    def get_mapping_by_yandex_uid(self, uid: str) -> EventMapping | None:
        row = self.conn.execute(
            "SELECT * FROM event_map WHERE yandex_uid = ?", (uid,)
        ).fetchone()
        return self._row_to_mapping(row) if row else None

    def get_mapping_by_google_id(self, google_id: str) -> EventMapping | None:
        row = self.conn.execute(
            "SELECT * FROM event_map WHERE google_event_id = ?", (google_id,)
        ).fetchone()
        return self._row_to_mapping(row) if row else None

    def get_all_mappings(self) -> list[EventMapping]:
        rows = self.conn.execute("SELECT * FROM event_map").fetchall()
        return [self._row_to_mapping(r) for r in rows]

    def create_mapping(self, mapping: EventMapping) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """INSERT INTO event_map
               (yandex_uid, yandex_href, yandex_etag, google_event_id, google_etag,
                content_hash, source, last_synced_at, is_recurring)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mapping.yandex_uid,
                mapping.yandex_href,
                mapping.yandex_etag,
                mapping.google_event_id,
                mapping.google_etag,
                mapping.content_hash,
                mapping.source,
                now,
                mapping.is_recurring,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def update_mapping(
        self,
        mapping_id: int,
        *,
        yandex_etag: str | None = None,
        yandex_href: str | None = None,
        google_event_id: str | None = None,
        google_etag: str | None = None,
        content_hash: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        updates = ["updated_at = ?", "last_synced_at = ?"]
        params: list = [now, now]

        if yandex_etag is not None:
            updates.append("yandex_etag = ?")
            params.append(yandex_etag)
        if yandex_href is not None:
            updates.append("yandex_href = ?")
            params.append(yandex_href)
        if google_event_id is not None:
            updates.append("google_event_id = ?")
            params.append(google_event_id)
        if google_etag is not None:
            updates.append("google_etag = ?")
            params.append(google_etag)
        if content_hash is not None:
            updates.append("content_hash = ?")
            params.append(content_hash)

        params.append(mapping_id)
        self.conn.execute(
            f"UPDATE event_map SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        self.conn.commit()

    def delete_mapping(self, mapping_id: int) -> None:
        self.conn.execute("DELETE FROM event_map WHERE id = ?", (mapping_id,))
        self.conn.commit()

    # === Sync State ===

    def get_sync_state(self, provider: str) -> SyncState | None:
        row = self.conn.execute(
            "SELECT * FROM sync_state WHERE provider = ?", (provider,)
        ).fetchone()
        if not row:
            return None
        return SyncState(
            provider=row["provider"],
            sync_token=row["sync_token"],
            calendar_ctag=row["calendar_ctag"],
            last_sync_at=row["last_sync_at"],
            last_full_sync_at=row["last_full_sync_at"],
        )

    def save_sync_state(self, state: SyncState) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO sync_state (provider, sync_token, calendar_ctag, last_sync_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(provider) DO UPDATE SET
                   sync_token = excluded.sync_token,
                   calendar_ctag = excluded.calendar_ctag,
                   last_sync_at = excluded.last_sync_at,
                   updated_at = excluded.updated_at""",
            (state.provider, state.sync_token, state.calendar_ctag, state.last_sync_at, now),
        )
        self.conn.commit()

    # === Sync Log ===

    def start_sync_log(self, direction: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            "INSERT INTO sync_log (started_at, direction) VALUES (?, ?)",
            (now, direction),
        )
        self.conn.commit()
        return cursor.lastrowid

    def finish_sync_log(
        self,
        log_id: int,
        *,
        events_created: int = 0,
        events_updated: int = 0,
        events_deleted: int = 0,
        conflicts_resolved: int = 0,
        errors: str | None = None,
        status: str = "success",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """UPDATE sync_log SET
               finished_at = ?, events_created = ?, events_updated = ?,
               events_deleted = ?, conflicts_resolved = ?, errors = ?, status = ?
               WHERE id = ?""",
            (now, events_created, events_updated, events_deleted, conflicts_resolved, errors, status, log_id),
        )
        self.conn.commit()

    # === Helpers ===

    @staticmethod
    def _row_to_mapping(row: sqlite3.Row) -> EventMapping:
        return EventMapping(
            id=row["id"],
            yandex_uid=row["yandex_uid"],
            yandex_href=row["yandex_href"],
            yandex_etag=row["yandex_etag"],
            google_event_id=row["google_event_id"],
            google_etag=row["google_etag"],
            content_hash=row["content_hash"],
            source=row["source"],
            last_synced_at=row["last_synced_at"],
            is_recurring=bool(row["is_recurring"]),
        )
