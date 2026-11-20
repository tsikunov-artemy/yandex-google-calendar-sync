from __future__ import annotations

from datetime import datetime
from typing import Protocol

from src.models.event import CalendarEvent


class CalendarProvider(Protocol):
    """Interface for calendar providers."""

    def connect(self) -> None:
        """Establish connection to the calendar service."""
        ...

    def get_events(self, time_min: datetime, time_max: datetime) -> list[CalendarEvent]:
        """Fetch events within the given time range."""
        ...

    def get_calendar_ctag(self) -> str | None:
        """Get calendar ctag/sync token for change detection. Returns None if not supported."""
        ...

    def create_event(self, event: CalendarEvent) -> str:
        """Create a new event. Returns the provider-specific event ID."""
        ...

    def update_event(self, provider_id: str, event: CalendarEvent) -> str:
        """Update an existing event. Returns the updated etag."""
        ...

    def delete_event(self, provider_id: str) -> None:
        """Delete an event by its provider-specific ID."""
        ...
