from __future__ import annotations

import structlog

from src.config import ConflictStrategy
from src.models.event import CalendarEvent, ChangeType, EventChange

log = structlog.get_logger()


def find_conflicts(
    yandex_changes: list[EventChange],
    google_changes: list[EventChange],
) -> list[tuple[EventChange, EventChange]]:
    """
    Find events that were modified in both Yandex and Google.
    Returns pairs of (yandex_change, google_change) that conflict.
    """
    # Build lookup: yandex_uid -> yandex change (for modifications only)
    yandex_by_uid: dict[str, EventChange] = {}
    for change in yandex_changes:
        if change.change_type == ChangeType.MODIFIED and change.event:
            yandex_by_uid[change.event.uid] = change

    # Build lookup: google changes that have a corresponding yandex_uid via mapping
    # We need the caller to have enriched google changes with yandex_uid info
    # For now, we match by UID (iCalUID in Google matches Yandex UID)
    google_by_uid: dict[str, EventChange] = {}
    for change in google_changes:
        if change.change_type == ChangeType.MODIFIED and change.event:
            google_by_uid[change.event.uid] = change

    conflicts = []
    for uid, y_change in yandex_by_uid.items():
        if uid in google_by_uid:
            conflicts.append((y_change, google_by_uid[uid]))
            log.info("conflict.detected", uid=uid, summary=y_change.event.summary if y_change.event else "?")

    return conflicts


def resolve_conflicts(
    conflicts: list[tuple[EventChange, EventChange]],
    strategy: ConflictStrategy,
) -> list[tuple[CalendarEvent, str]]:
    """
    Resolve conflicts based on strategy.
    Returns list of (winning_event, side_to_update) where side_to_update is 'yandex' or 'google'.
    """
    resolved = []

    for yandex_change, google_change in conflicts:
        y_event = yandex_change.event
        g_event = google_change.event

        if not y_event or not g_event:
            continue

        if strategy == ConflictStrategy.YANDEX_WINS:
            resolved.append((y_event, "google"))
            log.info("conflict.resolved", strategy="yandex_wins", uid=y_event.uid)

        elif strategy == ConflictStrategy.GOOGLE_WINS:
            resolved.append((g_event, "yandex"))
            log.info("conflict.resolved", strategy="google_wins", uid=y_event.uid)

        elif strategy == ConflictStrategy.LAST_WRITE_WINS:
            y_updated = y_event.updated or y_event.created
            g_updated = g_event.updated or g_event.created

            if y_updated and g_updated and g_updated > y_updated:
                resolved.append((g_event, "yandex"))
                log.info("conflict.resolved", strategy="last_write_wins", winner="google", uid=y_event.uid)
            else:
                # Yandex wins by default if timestamps are equal or missing
                resolved.append((y_event, "google"))
                log.info("conflict.resolved", strategy="last_write_wins", winner="yandex", uid=y_event.uid)

        elif strategy == ConflictStrategy.SKIP:
            log.info("conflict.skipped", uid=y_event.uid)

    return resolved
