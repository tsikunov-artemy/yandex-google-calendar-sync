from __future__ import annotations

import structlog

from src.db.repository import EventMapping, Repository
from src.models.event import CalendarEvent, ChangeType, EventChange

log = structlog.get_logger()


def detect_yandex_changes(
    remote_events: list[CalendarEvent],
    repo: Repository,
) -> list[EventChange]:
    """
    Compare remote Yandex events with local state to detect changes.
    Returns list of EventChange (NEW, MODIFIED, DELETED).
    """
    changes: list[EventChange] = []
    mappings = repo.get_all_mappings()
    mappings_by_yandex_uid = {m.yandex_uid: m for m in mappings if m.yandex_uid}
    remote_uids = {event.uid for event in remote_events}

    for event in remote_events:
        if event.recurrence_id is not None:
            continue
        mapping = mappings_by_yandex_uid.get(event.uid)

        if mapping is None:
            # New event — not in our mapping yet
            changes.append(EventChange(event=event, change_type=ChangeType.NEW))
            log.debug("differ.yandex.new", uid=event.uid, summary=event.summary)
        else:
            # Check if changed by comparing content hash
            new_hash = event.content_hash()
            if new_hash != mapping.content_hash:
                changes.append(EventChange(event=event, change_type=ChangeType.MODIFIED))
                log.debug("differ.yandex.modified", uid=event.uid, summary=event.summary)

    # Detect deletions: mappings that have yandex_uid but uid is not in remote anymore
    for mapping in mappings:
        if mapping.yandex_uid and mapping.yandex_uid not in remote_uids:
            changes.append(
                EventChange(
                    event=None,
                    change_type=ChangeType.DELETED,
                    yandex_uid=mapping.yandex_uid,
                    google_event_id=mapping.google_event_id,
                )
            )
            log.debug("differ.yandex.deleted", uid=mapping.yandex_uid)

    log.info(
        "differ.yandex.summary",
        new=sum(1 for c in changes if c.change_type == ChangeType.NEW),
        modified=sum(1 for c in changes if c.change_type == ChangeType.MODIFIED),
        deleted=sum(1 for c in changes if c.change_type == ChangeType.DELETED),
    )
    return changes


def detect_google_changes(
    remote_events: list[CalendarEvent],
    repo: Repository,
) -> list[EventChange]:
    """
    Compare remote Google events with local state to detect changes.
    Returns list of EventChange (NEW, MODIFIED, DELETED).
    """
    changes: list[EventChange] = []
    mappings = repo.get_all_mappings()
    mappings_by_google_id = {m.google_event_id: m for m in mappings if m.google_event_id}

    for event in remote_events:
        google_id = event.provider_id or event.uid

        # Cancelled events from incremental sync = deletions
        if event.status.value == "cancelled":
            mapping = mappings_by_google_id.get(google_id)
            if mapping:
                changes.append(
                    EventChange(
                        event=None,
                        change_type=ChangeType.DELETED,
                        yandex_uid=mapping.yandex_uid,
                        google_event_id=google_id,
                    )
                )
                log.debug("differ.google.deleted_cancelled", google_id=google_id)
            continue

        mapping = mappings_by_google_id.get(google_id)

        if mapping is None:
            # New event
            changes.append(EventChange(event=event, change_type=ChangeType.NEW))
            log.debug("differ.google.new", google_id=google_id, summary=event.summary)
        else:
            # Check if changed
            new_hash = event.content_hash()
            if new_hash != mapping.content_hash:
                changes.append(EventChange(event=event, change_type=ChangeType.MODIFIED))
                log.debug("differ.google.modified", google_id=google_id, summary=event.summary)

    # A delta or a bounded date window can omit live events. Only explicit
    # cancellations above establish a Google deletion.
    log.info(
        "differ.google.summary",
        new=sum(1 for c in changes if c.change_type == ChangeType.NEW),
        modified=sum(1 for c in changes if c.change_type == ChangeType.MODIFIED),
        deleted=sum(1 for c in changes if c.change_type == ChangeType.DELETED),
    )
    return changes
