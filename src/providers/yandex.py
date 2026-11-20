from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import caldav
import structlog
from icalendar import Calendar as iCalCalendar
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import Settings
from src.models.event import CalendarEvent, EventStatus

log = structlog.get_logger()


class YandexProvider:
    """Yandex Calendar provider via CalDAV protocol."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: caldav.DAVClient | None = None
        self.calendar: caldav.Calendar | None = None

    def connect(self) -> None:
        """Connect to Yandex CalDAV server and find the target calendar."""
        log.info("yandex.connecting", url=self.settings.yandex_caldav_url)
        self.client = caldav.DAVClient(
            url=self.settings.yandex_caldav_url,
            username=self.settings.yandex_username,
            password=self.settings.yandex_password,
        )
        principal = self.client.principal()
        calendars = principal.calendars()

        if not calendars:
            raise RuntimeError("No calendars found in Yandex account")

        if self.settings.yandex_calendar_name:
            for cal in calendars:
                if cal.name == self.settings.yandex_calendar_name:
                    self.calendar = cal
                    break
            if not self.calendar:
                names = [c.name for c in calendars]
                raise RuntimeError(
                    f"Calendar '{self.settings.yandex_calendar_name}' not found. Available: {names}"
                )
        else:
            self.calendar = calendars[0]

        log.info("yandex.connected", calendar=self.calendar.name, total_calendars=len(calendars))

    def get_calendar_ctag(self) -> str | None:
        """Get calendar ctag for change detection."""
        if not self.calendar:
            return None
        try:
            props = self.calendar.get_properties([caldav.dav.GetCTag()])
            for val in props.values():
                if val:
                    return str(val)
        except Exception:
            log.warning("yandex.ctag_not_supported")
        return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
    def get_events(self, time_min: datetime, time_max: datetime) -> list[CalendarEvent]:
        """Fetch all events within the given time range."""
        if not self.calendar:
            raise RuntimeError("Not connected. Call connect() first.")

        log.info("yandex.fetching_events", time_min=time_min.isoformat(), time_max=time_max.isoformat())
        self._rate_limit()

        return self.read_components(time_min, time_max, expand=False)

    def read_components(self, start: datetime, end: datetime, *, expand: bool) -> list[CalendarEvent]:
        resources = self.calendar.search(start=start, end=end, event=True, expand=expand)
        events = []
        for resource in resources:
            components = iCalCalendar.from_ical(resource.data).walk("VEVENT")
            if not components:
                raise ValueError("CalDAV returned an unreadable event resource")
            events.extend(self._parse_component(component, resource) for component in components)
        return events

    def confirmed_absent(self, uid: str, href: str | None) -> bool:
        # A time-window search also omits events that still exist outside the window.
        # Check the recorded resource, then the UID in case the resource moved.
        if href:
            resource = caldav.Event(client=self.client, url=href, parent=self.calendar)
            try:
                resource.load()
            except caldav.error.NotFoundError:
                pass
            else:
                return False
        return self._find_event_by_uid(uid) is None

    def get_events_with_etags(self, time_min: datetime, time_max: datetime) -> list[CalendarEvent]:
        """Fetch events with their etags for change detection."""
        return self.get_events(time_min, time_max)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
    def create_event(self, event: CalendarEvent) -> str:
        """Create a new event in Yandex Calendar. Returns the UID."""
        if not self.calendar:
            raise RuntimeError("Not connected. Call connect() first.")

        self._rate_limit()
        ical_str = self._event_to_ical(event)
        log.info("yandex.creating_event", summary=event.summary)

        created = self.calendar.save_event(ical_str)
        log.info("yandex.created", uid=event.uid)
        return event.uid

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
    def update_event(self, uid: str, event: CalendarEvent, href: str | None = None) -> str:
        """Update an existing event. Returns the new etag."""
        if not self.calendar:
            raise RuntimeError("Not connected. Call connect() first.")

        self._rate_limit()
        log.info("yandex.updating_event", uid=uid, summary=event.summary)

        # Find the event by UID
        existing = self._find_event_by_uid(uid)
        if not existing:
            log.warning("yandex.event_not_found_for_update", uid=uid)
            # Fallback: create instead
            return self.create_event(event)

        # Update the existing event's iCal data
        ical_str = self._event_to_ical(event)
        existing.data = ical_str
        existing.save()

        log.info("yandex.updated", uid=uid)
        return getattr(existing, "etag", "") or ""

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
    def delete_event(self, uid: str) -> None:
        """Delete an event by its UID."""
        if not self.calendar:
            raise RuntimeError("Not connected. Call connect() first.")

        self._rate_limit()
        log.info("yandex.deleting_event", uid=uid)

        existing = self._find_event_by_uid(uid)
        if existing:
            existing.delete()
            log.info("yandex.deleted", uid=uid)
        else:
            log.warning("yandex.event_not_found_for_delete", uid=uid)

    def _find_event_by_uid(self, uid: str) -> caldav.Event | None:
        """Find a CalDAV event by its UID."""
        try:
            return self.calendar.event_by_uid(uid)
        except caldav.error.NotFoundError:
            return None

    def _parse_component(self, component, raw_event: caldav.Event) -> CalendarEvent:
        uid = str(component["uid"])
        original = component.get("recurrence-id")
        # Parse start/end times
        dtstart = component.get("dtstart") or original
        dtend = component.get("dtend")
        if not dtstart:
            raise ValueError("Missing DTSTART")

        start_val = dtstart.dt
        duration = component.get("duration")
        default_duration = timedelta(0) if isinstance(start_val, datetime) else timedelta(days=1)
        end_val = dtend.dt if dtend else start_val + (duration.dt if duration else default_duration)
        if isinstance(start_val, datetime) and start_val.tzinfo is None:
            start_val = start_val.replace(tzinfo=ZoneInfo("Europe/Moscow"))
        if isinstance(end_val, datetime) and end_val.tzinfo is None:
            end_val = end_val.replace(tzinfo=ZoneInfo("Europe/Moscow"))
        all_day = isinstance(start_val, date) and not isinstance(start_val, datetime)

        # Determine timezone
        tz = "Europe/Moscow"
        if isinstance(start_val, datetime) and start_val.tzinfo:
            tz = str(start_val.tzinfo)

        # Parse status
        status_str = str(component.get("status", "CONFIRMED")).upper()
        status_map = {
            "CONFIRMED": EventStatus.CONFIRMED,
            "TENTATIVE": EventStatus.TENTATIVE,
            "CANCELLED": EventStatus.CANCELLED,
        }
        status = status_map.get(status_str, EventStatus.CONFIRMED)

        # Parse RRULE
        rrule = None
        is_recurring = False
        rrule_prop = component.get("rrule")
        if rrule_prop:
            is_recurring = True
            rrule = rrule_prop.to_ical().decode("utf-8")

        # Parse EXDATE (excluded dates from recurring events)
        exdates = []
        exdate_prop = component.get("exdate")
        if exdate_prop:
            if not isinstance(exdate_prop, list):
                exdate_prop = [exdate_prop]
            for exdate_item in exdate_prop:
                dt_values = exdate_item.to_ical().decode("utf-8")
                params = exdate_item.params
                if "TZID" in params:
                    exdates.append(f"EXDATE;TZID={params['TZID']}:{dt_values}")
                elif "VALUE" in params and str(params["VALUE"]).upper() == "DATE":
                    exdates.append(f"EXDATE;VALUE=DATE:{dt_values}")
                else:
                    exdates.append(f"EXDATE:{dt_values}")

        # Parse attendees
        attendees = []
        participants = component.get("attendee", [])
        for att in participants if isinstance(participants, list) else [participants]:
            email = str(att).replace("mailto:", "").replace("MAILTO:", "")
            if email:
                attendees.append(email)

        # Parse reminders (VALARM)
        reminders = []
        for alarm in component.walk("VALARM"):
            trigger = alarm.get("trigger")
            if trigger and hasattr(trigger, "dt"):
                td = trigger.dt
                if hasattr(td, "total_seconds"):
                    minutes = abs(int(td.total_seconds())) // 60
                    reminders.append(minutes)

        # Parse timestamps
        created = None
        created_prop = component.get("created")
        if created_prop:
            created = created_prop.dt if hasattr(created_prop, "dt") else None

        updated = None
        last_mod = component.get("last-modified")
        if last_mod:
            updated = last_mod.dt if hasattr(last_mod, "dt") else None

        description = str(component.get("description", ""))
        for field in ("CONFERENCE", "X-TELEMOST-CONFERENCE"):
            links = component.get(field, [])
            for link in links if isinstance(links, list) else [links]:
                url = str(link)
                if url.startswith(("https://", "http://")) and url not in description:
                    description = (description + "\n" + url).strip()

        return CalendarEvent(
            uid=uid,
            provider_id=uid,
            summary=str(component.get("summary", "")),
            description=description or None,
            recurrence_id=original.dt.isoformat() if original else None,
            location=str(component.get("location", "")) or None,
            start=start_val,
            end=end_val,
            all_day=all_day,
            timezone=tz,
            status=status,
            is_recurring=is_recurring,
            rrule=rrule,
            exdates=exdates,
            organizer=str(component.get("organizer", "")).replace("mailto:", "") or None,
            attendees=attendees,
            reminders_minutes=reminders,
            created=created,
            updated=updated,
            etag=getattr(raw_event, "etag", None),
            href=str(raw_event.url) if raw_event.url else None,
        )

    def _event_to_ical(self, event: CalendarEvent) -> str:
        """Convert a CalendarEvent to iCal string for creating/updating in Yandex."""
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//CalendarSync//EN",
            "BEGIN:VEVENT",
            f"UID:{event.uid}",
            f"SUMMARY:{event.summary}",
        ]

        if event.description:
            # Escape special characters in iCal
            desc = event.description.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,")
            lines.append(f"DESCRIPTION:{desc}")
        if event.location:
            loc = event.location.replace(",", "\\,")
            lines.append(f"LOCATION:{loc}")

        # Format dates
        if event.all_day:
            start_str = self._format_date(event.start)
            end_str = self._format_date(event.end)
            lines.append(f"DTSTART;VALUE=DATE:{start_str}")
            lines.append(f"DTEND;VALUE=DATE:{end_str}")
        else:
            start_str = self._format_datetime(event.start)
            end_str = self._format_datetime(event.end)
            lines.append(f"DTSTART:{start_str}")
            lines.append(f"DTEND:{end_str}")

        # Status
        status_map = {
            EventStatus.CONFIRMED: "CONFIRMED",
            EventStatus.TENTATIVE: "TENTATIVE",
            EventStatus.CANCELLED: "CANCELLED",
        }
        lines.append(f"STATUS:{status_map.get(event.status, 'CONFIRMED')}")

        # RRULE
        if event.rrule:
            lines.append(f"RRULE:{event.rrule}")

        # EXDATE
        for exdate_str in event.exdates:
            lines.append(exdate_str)

        # Timestamps
        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines.append(f"DTSTAMP:{now}")

        # Reminders
        for minutes in event.reminders_minutes:
            lines.extend([
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                "DESCRIPTION:Reminder",
                f"TRIGGER:-PT{minutes}M",
                "END:VALARM",
            ])

        lines.extend(["END:VEVENT", "END:VCALENDAR"])
        return "\r\n".join(lines)

    @staticmethod
    def _format_datetime(dt: datetime | date) -> str:
        if isinstance(dt, datetime):
            if dt.tzinfo:
                return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            return dt.strftime("%Y%m%dT%H%M%SZ")
        return dt.strftime("%Y%m%d")

    @staticmethod
    def _format_date(dt: datetime | date) -> str:
        if isinstance(dt, datetime):
            return dt.strftime("%Y%m%d")
        return dt.strftime("%Y%m%d")

    def _rate_limit(self) -> None:
        """Simple rate limiting for Yandex CalDAV."""
        delay = self.settings.yandex_request_delay_seconds
        if delay > 0:
            time.sleep(delay)
