"""
Adaptive sync scheduler.

Instead of fixed-interval polling, adjusts frequency based on
proximity to the next calendar event.

Typical meeting start times: :00 and :30.
Updates often arrive 3-5 minutes before a meeting.

Schedule:
    Working hours           → sync every 30 sec (near-instant)
    non-working hours       → sync every 5 min
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import structlog

from src.models.event import CalendarEvent

log = structlog.get_logger()

# Intervals in seconds
INTERVAL_HOT = 60          # 1 min — working hours
INTERVAL_WARM = 60         # 1 min — working hours
INTERVAL_MILD = 60         # 1 min — working hours
INTERVAL_NORMAL = 60       # 1 min — working hours
INTERVAL_IDLE = 300        # 5 min — non-working hours


def compute_next_sync_delay(
    events: list[CalendarEvent],
    now: datetime | None = None,
    work_start_hour: int = 5,
    work_end_hour: int = 18,
) -> int:
    """
    Compute how many seconds to wait before the next sync,
    based on proximity to the nearest upcoming event.

    Args:
        events: List of calendar events (from the most recent sync).
        now: Current time (UTC). Defaults to utcnow.
        work_start_hour: Start of working hours (UTC), default 5 (=08:00 MSK).
        work_end_hour: End of working hours (UTC), default 18 (=21:00 MSK).

    Returns:
        Delay in seconds until next sync.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Check if we're in working hours
    if not _is_working_time(now, work_start_hour, work_end_hour):
        log.debug("scheduler.idle", reason="non_working_hours")
        return INTERVAL_IDLE

    # Find time to the nearest upcoming event
    minutes_to_next = _minutes_to_next_event(events, now)

    if minutes_to_next is None:
        # No upcoming events in the next hour.
        # But check proximity to typical meeting slots (:00 and :30) —
        # meetings we don't know about yet might start there.
        minutes_to_slot = _minutes_to_typical_slot(now)
        if minutes_to_slot is not None and minutes_to_slot <= 5:
            log.debug("scheduler.hot_slot", minutes_to_slot=round(minutes_to_slot, 1))
            return INTERVAL_HOT
        log.debug("scheduler.normal", reason="no_upcoming_events")
        return INTERVAL_NORMAL

    if minutes_to_next <= 5:
        log.debug("scheduler.hot", minutes_to_next=round(minutes_to_next, 1))
        return INTERVAL_HOT
    elif minutes_to_next <= 15:
        log.debug("scheduler.warm", minutes_to_next=round(minutes_to_next, 1))
        return INTERVAL_WARM
    elif minutes_to_next <= 30:
        log.debug("scheduler.mild", minutes_to_next=round(minutes_to_next, 1))
        return INTERVAL_MILD
    else:
        log.debug("scheduler.normal", minutes_to_next=round(minutes_to_next, 1))
        return INTERVAL_NORMAL


def _is_working_time(now: datetime, work_start_hour: int, work_end_hour: int) -> bool:
    """Check if current time is within working hours (weekdays only)."""
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return work_start_hour <= now.hour < work_end_hour


def _minutes_to_next_event(events: list[CalendarEvent], now: datetime) -> float | None:
    """
    Find minutes until the next upcoming event starts.
    Considers events starting in the next 60 minutes,
    plus events that started up to 10 minutes ago (late updates).
    Returns None if nothing relevant.
    """
    min_minutes = None

    for event in events:
        if event.all_day:
            continue

        event_start = event.start
        if isinstance(event_start, date) and not isinstance(event_start, datetime):
            continue

        # Ensure timezone awareness
        if event_start.tzinfo is None:
            event_start = event_start.replace(tzinfo=timezone.utc)

        diff_minutes = (event_start - now).total_seconds() / 60.0

        # Skip events that started more than 10 minutes ago
        if diff_minutes < -10:
            continue
        # Skip events more than 60 minutes in the future
        if diff_minutes > 60:
            continue
        # Clamp: event already started → treat as 0
        if diff_minutes < 0:
            diff_minutes = 0

        if min_minutes is None or diff_minutes < min_minutes:
            min_minutes = diff_minutes

    return min_minutes


def _minutes_to_typical_slot(now: datetime) -> float | None:
    """
    Minutes until the next :00 or :30 meeting slot.
    Returns None if more than 10 minutes away.
    """
    minute = now.minute
    second = now.second
    fractional_minute = minute + second / 60.0

    # Next :00 and :30 from current position
    if fractional_minute < 30:
        to_30 = 30 - fractional_minute
        to_00 = 60 - fractional_minute
    else:
        to_30 = 90 - fractional_minute  # next :30 is in the next hour
        to_00 = 60 - fractional_minute

    nearest = min(to_30, to_00)
    return nearest if nearest <= 10 else None
