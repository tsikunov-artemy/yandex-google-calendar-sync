from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class EventStatus(str, Enum):
    CONFIRMED = "confirmed"
    TENTATIVE = "tentative"
    CANCELLED = "cancelled"


class ChangeType(str, Enum):
    NEW = "new"
    MODIFIED = "modified"
    DELETED = "deleted"


class CalendarEvent(BaseModel):
    """Unified event model for exchange between providers."""

    # Identification
    uid: str
    provider_id: str | None = None

    # Core fields
    summary: str = ""
    description: str | None = None
    location: str | None = None

    # Time
    start: datetime | date
    end: datetime | date
    all_day: bool = False
    timezone: str = "Europe/Moscow"

    # Status
    status: EventStatus = EventStatus.CONFIRMED

    # Recurrence
    is_recurring: bool = False
    rrule: str | None = None
    recurrence_id: str | None = None
    exdates: list[str] = Field(default_factory=list)  # iCal-format EXDATE strings

    # Participants
    organizer: str | None = None
    attendees: list[str] = Field(default_factory=list)

    # Reminders
    reminders_minutes: list[int] = Field(default_factory=list)

    # Metadata
    created: datetime | None = None
    updated: datetime | None = None
    etag: str | None = None
    href: str | None = None  # CalDAV href

    def content_hash(self) -> str:
        """SHA256 hash of key fields to detect changes."""
        data = json.dumps(
            {
                "summary": self.summary,
                "description": self.description or "",
                "location": self.location or "",
                "start": self.start.isoformat() if self.start else "",
                "end": self.end.isoformat() if self.end else "",
                "all_day": self.all_day,
                "status": self.status.value,
                "rrule": self.rrule or "",
                "exdates": sorted(self.exdates),
                "attendees": sorted(self.attendees),
                "reminders_minutes": sorted(self.reminders_minutes),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(data.encode()).hexdigest()


class EventChange(BaseModel):
    """Represents a detected change in a calendar event."""

    event: CalendarEvent | None = None  # None for deletions where full event is unknown
    change_type: ChangeType
    # For deletions, we need the mapping info
    yandex_uid: str | None = None
    google_event_id: str | None = None
