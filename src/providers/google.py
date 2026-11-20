from __future__ import annotations

import os
from datetime import date, datetime, timezone

import structlog
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import Settings
from src.models.event import CalendarEvent, EventStatus

log = structlog.get_logger()

SCOPES = ["https://www.googleapis.com/auth/calendar"]


class GoogleProvider:
    """Google Calendar provider via REST API."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.service = None
        self.creds: Credentials | None = None

    def connect(self) -> None:
        """Authenticate and connect to Google Calendar API."""
        log.info("google.connecting")
        self.creds = self._get_credentials()
        self.service = build("calendar", "v3", credentials=self.creds)

        # If a calendar name is specified, find or create it
        if self.settings.google_calendar_name:
            cal_id = self._ensure_calendar(self.settings.google_calendar_name)
            self.settings.google_calendar_id = cal_id

        log.info("google.connected", calendar_id=self.settings.google_calendar_id)

    def _get_credentials(self) -> Credentials:
        """Load or refresh Google OAuth2 credentials."""
        creds = None
        token_path = self.settings.google_token_path

        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)

        if creds and creds.expired and creds.refresh_token:
            log.info("google.refreshing_token")
            creds.refresh(Request())
            self._save_token(creds)
        elif not creds or not creds.valid:
            if not os.path.exists(self.settings.google_credentials_path):
                raise RuntimeError(
                    f"Google credentials file not found: {self.settings.google_credentials_path}. "
                    "Run 'python scripts/setup_google_oauth.py' first."
                )
            flow = InstalledAppFlow.from_client_secrets_file(self.settings.google_credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
            self._save_token(creds)

        return creds

    def _save_token(self, creds: Credentials) -> None:
        with open(self.settings.google_token_path, "w") as f:
            f.write(creds.to_json())

    def _ensure_calendar(self, name: str) -> str:
        """Find an existing calendar by name, or create a new one. Returns calendarId."""
        # List all calendars
        calendars = self.service.calendarList().list().execute()
        for cal in calendars.get("items", []):
            if cal.get("summary") == name:
                log.info("google.calendar_found", name=name, calendar_id=cal["id"])
                return cal["id"]

        # Not found — create
        log.info("google.calendar_creating", name=name)
        body = {
            "summary": name,
            "timeZone": "Europe/Moscow",
        }
        created = self.service.calendars().insert(body=body).execute()
        cal_id = created["id"]
        log.info("google.calendar_created", name=name, calendar_id=cal_id)
        return cal_id

    def get_calendar_ctag(self) -> str | None:
        """Google uses syncToken instead of ctag. Returns None here."""
        return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=30))
    def get_events(self, time_min: datetime, time_max: datetime) -> list[CalendarEvent]:
        """Fetch events within the given time range."""
        if not self.service:
            raise RuntimeError("Not connected. Call connect() first.")

        log.info("google.fetching_events", time_min=time_min.isoformat(), time_max=time_max.isoformat())

        events = []
        page_token = None

        while True:
            result = (
                self.service.events()
                .list(
                    calendarId=self.settings.google_calendar_id,
                    timeMin=time_min.isoformat(),
                    timeMax=time_max.isoformat(),
                    singleEvents=False,  # Get recurring event masters
                    maxResults=250,
                    pageToken=page_token,
                    showDeleted=False,
                )
                .execute()
            )

            for item in result.get("items", []):
                try:
                    event = self._parse_event(item)
                    if event:
                        events.append(event)
                except Exception as e:
                    log.warning("google.parse_error", error=str(e), event_id=item.get("id"))

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        log.info("google.fetched", count=len(events))
        return events

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=30))
    def get_events_incremental(self, sync_token: str) -> tuple[list[CalendarEvent], str | None, bool]:
        """
        Fetch changed events using syncToken.
        Returns: (events, new_sync_token, token_expired).
        If token_expired is True, caller should do a full sync.
        """
        if not self.service:
            raise RuntimeError("Not connected. Call connect() first.")

        try:
            events = []
            page_token = None
            new_sync_token = None

            while True:
                result = (
                    self.service.events()
                    .list(
                        calendarId=self.settings.google_calendar_id,
                        syncToken=sync_token,
                        showDeleted=True,
                        maxResults=250,
                        pageToken=page_token,
                    )
                    .execute()
                )

                for item in result.get("items", []):
                    try:
                        event = self._parse_event(item)
                        if event:
                            events.append(event)
                    except Exception as e:
                        log.warning("google.parse_error", error=str(e), event_id=item.get("id"))

                page_token = result.get("nextPageToken")
                new_sync_token = result.get("nextSyncToken")
                if not page_token:
                    break

            return events, new_sync_token, False

        except HttpError as e:
            if e.resp.status == 410:
                log.warning("google.sync_token_expired")
                return [], None, True
            raise

    def get_sync_token(self, time_min: datetime, time_max: datetime) -> str | None:
        """Do a full list to get the initial syncToken."""
        if not self.service:
            raise RuntimeError("Not connected. Call connect() first.")

        page_token = None
        sync_token = None

        while True:
            result = (
                self.service.events()
                .list(
                    calendarId=self.settings.google_calendar_id,
                    timeMin=time_min.isoformat(),
                    timeMax=time_max.isoformat(),
                    singleEvents=False,
                    maxResults=250,
                    pageToken=page_token,
                )
                .execute()
            )
            page_token = result.get("nextPageToken")
            sync_token = result.get("nextSyncToken")
            if not page_token:
                break

        return sync_token

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=30), reraise=True)
    def create_event(self, event: CalendarEvent) -> str:
        """Create a new event in Google Calendar using import (handles iCalUID properly).
        Returns the Google event ID."""
        if not self.service:
            raise RuntimeError("Not connected. Call connect() first.")

        body = self._event_to_google(event)
        log.info("google.creating_event", summary=event.summary)

        try:
            result = self._import_event(body)
        except HttpError as e:
            if e.resp.status != 400:
                raise
            if "sequence" in str(e).lower():
                # Event with this UID already exists in Google with a higher sequence
                # (e.g. mapping was lost) — import over it using its sequence
                existing = self._find_by_ical_uid(body["iCalUID"])
                if not existing:
                    raise
                body["sequence"] = existing.get("sequence", 0)
                log.warning("google.sequence_adjusted", summary=event.summary, sequence=body["sequence"])
            else:
                # Google rejects some UIDs (e.g. of purged events) — import under a derived UID
                body["iCalUID"] = f"{event.uid}-gsync"
                log.warning("google.uid_rejected", summary=event.summary, uid=event.uid)
            result = self._import_event(body)

        google_id = result["id"]
        log.info("google.created", google_id=google_id)
        return google_id

    def _import_event(self, body: dict) -> dict:
        # Use import_ instead of insert:
        # - Designed for importing external events with iCalUID
        # - Won't send invitations to attendees
        # - Handles previously deleted events with same UID
        return (
            self.service.events()
            .import_(
                calendarId=self.settings.google_calendar_id,
                body=body,
            )
            .execute()
        )

    def _find_by_ical_uid(self, ical_uid: str) -> dict | None:
        result = (
            self.service.events()
            .list(calendarId=self.settings.google_calendar_id, iCalUID=ical_uid, showDeleted=True)
            .execute()
        )
        items = result.get("items", [])
        return items[0] if items else None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=30))
    def update_event(self, google_event_id: str, event: CalendarEvent) -> str:
        """Update an existing event. Returns the new etag."""
        if not self.service:
            raise RuntimeError("Not connected. Call connect() first.")

        body = self._event_to_google(event)
        body.pop("iCalUID")  # keep the event's own UID (may be a derived "-gsync" one)
        log.info("google.updating_event", google_id=google_event_id, summary=event.summary)

        result = (
            self.service.events()
            .update(
                calendarId=self.settings.google_calendar_id,
                eventId=google_event_id,
                body=body,
                sendUpdates="none",
            )
            .execute()
        )

        log.info("google.updated", google_id=google_event_id)
        return result.get("etag", "")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=30))
    def delete_event(self, google_event_id: str) -> None:
        """Delete an event by its Google event ID."""
        if not self.service:
            raise RuntimeError("Not connected. Call connect() first.")

        log.info("google.deleting_event", google_id=google_event_id)
        try:
            self.service.events().delete(
                calendarId=self.settings.google_calendar_id,
                eventId=google_event_id,
                sendUpdates="none",
            ).execute()
            log.info("google.deleted", google_id=google_event_id)
        except HttpError as e:
            if e.resp.status == 410:
                log.info("google.already_deleted", google_id=google_event_id)
            else:
                raise

    def _parse_event(self, item: dict) -> CalendarEvent | None:
        """Parse a Google Calendar API event into our unified model."""
        event_id = item.get("id", "")
        if not event_id:
            return None

        # Use iCalUID if available, otherwise use Google event ID
        uid = item.get("iCalUID", event_id)

        # Parse start/end
        start_data = item.get("start", {})
        end_data = item.get("end", {})

        all_day = "date" in start_data and "dateTime" not in start_data

        if all_day:
            start = date.fromisoformat(start_data["date"])
            end = date.fromisoformat(end_data.get("date", start_data["date"]))
        else:
            start_str = start_data.get("dateTime", "")
            end_str = end_data.get("dateTime", start_str)
            if not start_str:
                return None
            start = datetime.fromisoformat(start_str)
            end = datetime.fromisoformat(end_str)

        tz = start_data.get("timeZone", "Europe/Moscow")

        # Status
        status_str = item.get("status", "confirmed")
        status_map = {
            "confirmed": EventStatus.CONFIRMED,
            "tentative": EventStatus.TENTATIVE,
            "cancelled": EventStatus.CANCELLED,
        }
        status = status_map.get(status_str, EventStatus.CONFIRMED)

        # Recurrence
        recurrence = item.get("recurrence", [])
        is_recurring = bool(recurrence)
        rrule = None
        exdates = []
        for rule in recurrence:
            if rule.startswith("RRULE:"):
                rrule = rule[6:]  # Remove "RRULE:" prefix
            elif rule.startswith("EXDATE"):
                exdates.append(rule)

        # Attendees
        attendees = []
        for att in item.get("attendees", []):
            email = att.get("email", "")
            if email:
                attendees.append(email)

        # Organizer
        organizer = item.get("organizer", {}).get("email")

        # Reminders
        reminders = []
        rem_data = item.get("reminders", {})
        if not rem_data.get("useDefault", True):
            for override in rem_data.get("overrides", []):
                if override.get("method") == "popup":
                    reminders.append(override.get("minutes", 10))

        # Timestamps
        created = None
        if item.get("created"):
            try:
                created = datetime.fromisoformat(item["created"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        updated = None
        if item.get("updated"):
            try:
                updated = datetime.fromisoformat(item["updated"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        return CalendarEvent(
            uid=uid,
            provider_id=event_id,
            summary=item.get("summary", "(No title)"),
            description=item.get("description"),
            location=item.get("location"),
            start=start,
            end=end,
            all_day=all_day,
            timezone=tz,
            status=status,
            is_recurring=is_recurring,
            rrule=rrule,
            exdates=exdates,
            organizer=organizer,
            attendees=attendees,
            reminders_minutes=reminders,
            created=created,
            updated=updated,
            etag=item.get("etag"),
        )

    def _event_to_google(self, event: CalendarEvent) -> dict:
        """Convert a CalendarEvent to Google Calendar API format."""
        body: dict = {
            "summary": event.summary,
            "iCalUID": event.uid,
        }

        if event.description:
            body["description"] = event.description
        if event.location:
            body["location"] = event.location

        # Time
        if event.all_day:
            body["start"] = {"date": event.start.isoformat()}
            body["end"] = {"date": event.end.isoformat()}
        else:
            start = event.start
            end = event.end
            if isinstance(start, datetime):
                body["start"] = {"dateTime": start.isoformat(), "timeZone": event.timezone}
            else:
                body["start"] = {"date": start.isoformat()}
            if isinstance(end, datetime):
                body["end"] = {"dateTime": end.isoformat(), "timeZone": event.timezone}
            else:
                body["end"] = {"date": end.isoformat()}

        # Status
        status_map = {
            EventStatus.CONFIRMED: "confirmed",
            EventStatus.TENTATIVE: "tentative",
            EventStatus.CANCELLED: "cancelled",
        }
        body["status"] = status_map.get(event.status, "confirmed")

        # Recurrence
        if event.rrule or event.exdates:
            recurrence = []
            if event.rrule:
                recurrence.append(f"RRULE:{event.rrule}")
            recurrence.extend(event.exdates)
            body["recurrence"] = recurrence

        # Attendees (responseStatus=accepted so Google doesn't try to send invites)
        if event.attendees:
            body["attendees"] = [
                {"email": email, "responseStatus": "accepted"} for email in event.attendees
            ]

        # Append organizer info to description (can't set organizer via API)
        extra_info = []
        if event.organizer:
            extra_info.append(f"Организатор: {event.organizer}")
        if extra_info:
            desc = body.get("description", "") or ""
            separator = "\n\n---\n" if desc else ""
            body["description"] = desc + separator + "\n".join(extra_info)

        # Reminders
        if event.reminders_minutes:
            body["reminders"] = {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": m} for m in event.reminders_minutes],
            }

        return body
