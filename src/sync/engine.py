from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import structlog

from src.config import ConflictStrategy, Settings, SyncDirection
from src.db.engine import get_connection
from src.db.repository import EventMapping, Repository, SyncState
from src.models.event import ChangeType, EventChange
from src.providers.google import GoogleProvider
from src.providers.yandex import YandexProvider
from src.sync.conflict import find_conflicts, resolve_conflicts
from src.sync.mirror import GoogleMirror
from src.sync.differ import detect_google_changes, detect_yandex_changes

log = structlog.get_logger()


class SyncEngine:
    """Main synchronization engine between Yandex and Google Calendar."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.yandex = YandexProvider(settings)
        self.google = GoogleProvider(settings)
        self.conn = get_connection(settings.db_path)
        self.repo = Repository(self.conn)
        self.mirror = GoogleMirror(self.yandex, self.google, self.repo)
        self.last_fetched_events: list = []  # For adaptive scheduler

    def connect(self) -> None:
        """Connect to both calendar providers."""
        self.yandex.connect()
        self.google.connect()

    def run_sync(self) -> None:
        """Execute one sync cycle."""
        log_id = self.repo.start_sync_log(self.settings.sync_direction.value)
        stats = {"created": 0, "updated": 0, "deleted": 0, "conflicts": 0}
        errors = []

        try:
            log.info("sync.starting", direction=self.settings.sync_direction.value)

            now = datetime.now(timezone.utc)
            time_min = now - timedelta(days=self.settings.sync_window_days_past)
            time_max = now + timedelta(days=self.settings.sync_window_days_future)

            direction = self.settings.sync_direction

            # Fetch events from both sides
            components = []
            yandex_events = []
            google_events = []

            if direction in (SyncDirection.BIDIRECTIONAL, SyncDirection.YANDEX_TO_GOOGLE):
                components = self.yandex.get_events(time_min, time_max)
                yandex_events = [e for e in components if e.recurrence_id is None]

            if direction in (SyncDirection.BIDIRECTIONAL, SyncDirection.GOOGLE_TO_YANDEX):
                google_events = self._get_google_events(time_min, time_max)

            # Save for adaptive scheduler
            self.last_fetched_events = yandex_events + google_events

            # Detect changes
            yandex_changes = detect_yandex_changes(components, self.repo) if direction != SyncDirection.GOOGLE_TO_YANDEX else []
            google_changes = detect_google_changes(google_events, self.repo) if google_events else []

            # Handle conflicts (only in bidirectional mode)
            conflict_uids = set()
            if direction == SyncDirection.BIDIRECTIONAL:
                conflicts = find_conflicts(yandex_changes, google_changes)
                if conflicts:
                    resolved = resolve_conflicts(conflicts, self.settings.conflict_strategy)
                    stats["conflicts"] = len(resolved)

                    # Track conflicting UIDs so we skip them in normal processing
                    for y_change, g_change in conflicts:
                        if y_change.event:
                            conflict_uids.add(y_change.event.uid)

                    # Apply conflict resolutions
                    for winner_event, side_to_update in resolved:
                        try:
                            if side_to_update == "google":
                                self._apply_to_google(
                                    EventChange(event=winner_event, change_type=ChangeType.MODIFIED),
                                    stats,
                                )
                            elif side_to_update == "yandex":
                                self._apply_to_yandex(
                                    EventChange(event=winner_event, change_type=ChangeType.MODIFIED),
                                    stats,
                                )
                        except Exception as e:
                            errors.append(f"Conflict resolution error: {e}")
                            log.error("sync.conflict_resolution_error", error=str(e))

            # Apply Yandex → Google changes
            if direction in (SyncDirection.BIDIRECTIONAL, SyncDirection.YANDEX_TO_GOOGLE):
                for change in yandex_changes:
                    if change.event and change.event.uid in conflict_uids:
                        continue  # Already handled as conflict
                    try:
                        self._apply_to_google(change, stats)
                    except Exception as e:
                        errors.append(f"Yandex→Google error ({change.yandex_uid or '?'}): {e}")
                        log.error("sync.yandex_to_google_error", error=str(e))

            # Apply Google → Yandex changes
            if direction in (SyncDirection.BIDIRECTIONAL, SyncDirection.GOOGLE_TO_YANDEX):
                for change in google_changes:
                    if change.event and change.event.uid in conflict_uids:
                        continue
                    try:
                        self._apply_to_yandex(change, stats)
                    except Exception as e:
                        errors.append(f"Google→Yandex error ({change.google_event_id or '?'}): {e}")
                        log.error("sync.google_to_yandex_error", error=str(e))

            if direction == SyncDirection.YANDEX_TO_GOOGLE:
                self.mirror.sync(components, now, time_min, time_max, stats, errors)

            # Save sync state
            yandex_ctag = self.yandex.get_calendar_ctag()
            self.repo.save_sync_state(SyncState(
                provider="yandex",
                calendar_ctag=yandex_ctag,
                last_sync_at=now.isoformat(),
            ))

            log.info(
                "sync.completed",
                created=stats["created"],
                updated=stats["updated"],
                deleted=stats["deleted"],
                conflicts=stats["conflicts"],
                errors=len(errors),
            )

            self.repo.finish_sync_log(
                log_id,
                events_created=stats["created"],
                events_updated=stats["updated"],
                events_deleted=stats["deleted"],
                conflicts_resolved=stats["conflicts"],
                errors=json.dumps(errors) if errors else None,
                status="success" if not errors else "partial",
            )

        except Exception as e:
            log.error("sync.failed", error=str(e))
            self.repo.finish_sync_log(log_id, errors=json.dumps([str(e)]), status="error")
            raise

    def _get_google_events(self, time_min: datetime, time_max: datetime) -> list:
        """Get Google events, using incremental sync if possible."""
        state = self.repo.get_sync_state("google")

        if state and state.sync_token:
            events, new_token, expired = self.google.get_events_incremental(state.sync_token)
            if not expired:
                if new_token:
                    self.repo.save_sync_state(SyncState(
                        provider="google",
                        sync_token=new_token,
                        last_sync_at=datetime.now(timezone.utc).isoformat(),
                    ))
                return events
            log.info("sync.google_token_expired_full_sync")

        # Full sync
        events = self.google.get_events(time_min, time_max)
        sync_token = self.google.get_sync_token(time_min, time_max)
        self.repo.save_sync_state(SyncState(
            provider="google",
            sync_token=sync_token,
            last_sync_at=datetime.now(timezone.utc).isoformat(),
        ))
        return events

    def _apply_to_google(self, change: EventChange, stats: dict) -> None:
        """Apply a change from Yandex to Google Calendar."""
        if change.change_type == ChangeType.NEW and change.event:
            google_id = self.google.create_event(change.event)
            self.repo.create_mapping(EventMapping(
                id=None,
                yandex_uid=change.event.uid,
                yandex_href=change.event.href,
                yandex_etag=change.event.etag,
                google_event_id=google_id,
                google_etag=None,
                content_hash=change.event.content_hash(),
                source="yandex",
                last_synced_at="",
                is_recurring=change.event.is_recurring,
            ))
            stats["created"] += 1

        elif change.change_type == ChangeType.MODIFIED and change.event:
            mapping = self.repo.get_mapping_by_yandex_uid(change.event.uid)
            if mapping and mapping.google_event_id:
                new_etag = self.google.update_event(mapping.google_event_id, change.event)
                self.repo.update_mapping(
                    mapping.id,
                    google_etag=new_etag,
                    yandex_etag=change.event.etag,
                    content_hash=change.event.content_hash(),
                )
                stats["updated"] += 1
            else:
                # No mapping exists, create instead
                google_id = self.google.create_event(change.event)
                self.repo.create_mapping(EventMapping(
                    id=None,
                    yandex_uid=change.event.uid,
                    yandex_href=change.event.href,
                    yandex_etag=change.event.etag,
                    google_event_id=google_id,
                    google_etag=None,
                    content_hash=change.event.content_hash(),
                    source="yandex",
                    last_synced_at="",
                    is_recurring=change.event.is_recurring,
                ))
                stats["created"] += 1

        elif change.change_type == ChangeType.DELETED:
            uid = change.yandex_uid
            if uid:
                mapping = self.repo.get_mapping_by_yandex_uid(uid)
                if mapping and mapping.google_event_id:
                    if not self.yandex.confirmed_absent(uid, mapping.yandex_href):
                        return
                    self.google.delete_event(mapping.google_event_id)
                    self.repo.delete_mapping(mapping.id)
                    stats["deleted"] += 1

    def _apply_to_yandex(self, change: EventChange, stats: dict) -> None:
        """Apply a change from Google to Yandex Calendar."""
        if change.change_type == ChangeType.NEW and change.event:
            yandex_uid = self.yandex.create_event(change.event)
            google_id = change.event.provider_id or change.event.uid
            self.repo.create_mapping(EventMapping(
                id=None,
                yandex_uid=change.event.uid,
                yandex_href=None,
                yandex_etag=None,
                google_event_id=google_id,
                google_etag=change.event.etag,
                content_hash=change.event.content_hash(),
                source="google",
                last_synced_at="",
                is_recurring=change.event.is_recurring,
            ))
            stats["created"] += 1

        elif change.change_type == ChangeType.MODIFIED and change.event:
            google_id = change.event.provider_id or change.event.uid
            mapping = self.repo.get_mapping_by_google_id(google_id)
            if mapping and mapping.yandex_uid:
                new_etag = self.yandex.update_event(
                    mapping.yandex_uid,
                    change.event,
                    href=mapping.yandex_href,
                )
                self.repo.update_mapping(
                    mapping.id,
                    yandex_etag=new_etag,
                    google_etag=change.event.etag,
                    content_hash=change.event.content_hash(),
                )
                stats["updated"] += 1
            else:
                # No mapping, create instead
                self.yandex.create_event(change.event)
                self.repo.create_mapping(EventMapping(
                    id=None,
                    yandex_uid=change.event.uid,
                    yandex_href=None,
                    yandex_etag=None,
                    google_event_id=google_id,
                    google_etag=change.event.etag,
                    content_hash=change.event.content_hash(),
                    source="google",
                    last_synced_at="",
                    is_recurring=change.event.is_recurring,
                ))
                stats["created"] += 1

        elif change.change_type == ChangeType.DELETED:
            google_id = change.google_event_id
            if google_id:
                mapping = self.repo.get_mapping_by_google_id(google_id)
                if mapping and mapping.yandex_uid:
                    self.yandex.delete_event(mapping.yandex_uid)
                    self.repo.delete_mapping(mapping.id)
                    stats["deleted"] += 1

    def close(self) -> None:
        """Close database connection."""
        if self.conn:
            self.conn.close()
