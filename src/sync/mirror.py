"""Keep the existing Google event IDs when a Yandex meeting moves."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import structlog
from googleapiclient.errors import HttpError

from src.models.event import EventStatus

log = structlog.get_logger()
MOSCOW = ZoneInfo("Europe/Moscow")
FIELDS = "id,status,start,end,originalStartTime,recurringEventId,summary,location,description,etag"


def instant(value):
    if isinstance(value, str):
        if len(value) == 10:
            return value
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=MOSCOW)).astimezone(timezone.utc).isoformat()
    return value.isoformat()


def google_time(field):
    return field.get("dateTime") or field.get("date")


def difference(desired, actual):
    if desired["status"] == "cancelled":
        return {} if actual.get("status") == "cancelled" else {"status": "cancelled"}
    patch = {}
    for key in ("summary", "description", "location", "status"):
        if desired.get(key, "") != actual.get(key, ""):
            patch[key] = desired.get(key, "")
    for key in ("start", "end"):
        before = google_time(actual.get(key, {}))
        if not before or instant(before) != instant(google_time(desired[key])):
            patch[key] = desired[key]
    return patch


class GoogleMirror:
    def __init__(self, yandex, google, repo):
        self.yandex = yandex
        self.google = google
        self.repo = repo
        self.checked = {}
        self.next_audit = None

    @property
    def api(self):
        return self.google.service.events()

    @property
    def calendar_id(self):
        return self.google.settings.google_calendar_id

    def list_instances(self, start, end):
        request = self.api.list(calendarId=self.calendar_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
                                singleEvents=True, maxResults=2500, fields=f"nextPageToken,items({FIELDS})")
        result = {}
        while request is not None:
            response = request.execute()
            for item in response.get("items", []):
                parent = item.get("recurringEventId", item["id"])
                original = google_time(item.get("originalStartTime", {}))
                result[parent, instant(original) if original else None] = item
            request = self.api.list_next(request, response)
        return result

    def lookup(self, parent, original, tz):
        if original is None:
            try:
                return self.api.get(calendarId=self.calendar_id, eventId=parent, fields=FIELDS).execute()
            except HttpError as error:
                if error.resp.status not in (404, 410):
                    raise
                return None
        # The instances filter accepts timestamps, including for all-day meetings.
        if len(original) == 10:
            stamp = datetime.combine(date.fromisoformat(original), time.min, ZoneInfo(tz)).isoformat()
        else:
            stamp = original
        try:
            result = self.api.instances(calendarId=self.calendar_id, eventId=parent, originalStart=stamp,
                                        showDeleted=True, fields=f"items({FIELDS})").execute()
        except HttpError as error:
            if error.resp.status not in (404, 410):
                raise
            return None
        matches = [i for i in result.get("items", [])
                   if instant(google_time(i["originalStartTime"])) == original]
        if len(matches) > 1:
            raise ValueError("Ambiguous Google occurrence")
        return matches[0] if matches else None

    def restore_master(self, event, mapping):
        """Restore a missing mapped event (including the series behind an instance)."""
        parent = mapping.google_event_id
        item = self.lookup(parent, None, event.timezone)
        if item:
            body = self.google._event_to_google(event)
            body.pop("iCalUID")
            patch = difference(body, item)
            if event.is_recurring:
                current = self.api.get(calendarId=self.calendar_id, eventId=parent,
                                       fields="recurrence,etag").execute()
                if sorted(current.get("recurrence", [])) != sorted(body.get("recurrence", [])):
                    patch["recurrence"] = body["recurrence"]
            if patch:
                self.patch_item(item, patch)
        else:
            parent = self.google.create_event(event)
            self.repo.update_mapping(mapping.id, google_event_id=parent)
            mapping.google_event_id = parent
        return parent

    def patch_item(self, item, patch):
        request = self.api.patch(calendarId=self.calendar_id, eventId=item["id"], body=patch,
                                 sendUpdates="none", fields="id,etag")
        request.headers["If-Match"] = item["etag"]
        request.execute()

    def sync(self, components, now, start, end, stats, errors):
        masters = {event.uid: event for event in components if not event.recurrence_id}
        mappings = {m.yandex_uid: m for m in self.repo.get_all_mappings()}
        overrides = {}
        for event in components:
            if event.recurrence_id:
                original = instant(event.recurrence_id)
                if any(start.date().isoformat() <= stamp[:10] <= end.date().isoformat()
                       for stamp in (original, instant(event.start))):
                    overrides[event.uid, original] = event

        due = self.next_audit is None or now >= self.next_audit
        expected = {}
        actual = {}
        if due:
            today = now.astimezone(MOSCOW).replace(hour=0, minute=0, second=0, microsecond=0)
            until = today + timedelta(days=14)
            actual = self.list_instances(today, until)
            for event in self.yandex.read_components(today, until, expand=True):
                original = instant(event.recurrence_id) if event.recurrence_id else None
                expected[event.uid, original] = event

        for key, event in overrides.items():
            mapping = mappings.get(event.uid)
            visible = mapping and (mapping.google_event_id, key[1]) in actual
            if self.checked.get(key) != event.content_hash() or key in expected or visible:
                expected[key] = event

        for key, event in expected.items():
            try:
                mapping = mappings.get(event.uid)
                if not mapping or not mapping.google_event_id:
                    raise ValueError("Missing Google series or event mapping")
                parent = mapping.google_event_id
                item_key = (parent, key[1])
                item = actual.pop(item_key, None)
                if not item:
                    item = self.lookup(parent, key[1], event.timezone)
                if not item and event.status != EventStatus.CANCELLED:
                    parent = self.restore_master(masters[event.uid], mapping)
                    item = self.lookup(parent, key[1], event.timezone)
                    if not item:
                        raise ValueError("Google is missing an expected occurrence after restoring its series")
                if item:
                    patch = difference(self.google._event_to_google(event), item)
                    if patch:
                        self.patch_item(item, patch)
                        stats["updated"] += 1
                if key in overrides:
                    self.checked[key] = event.content_hash()
            except Exception as error:
                errors.append(f"Mirror {event.uid}/{key[1]}: {error}")
                log.error("mirror.repair_failed", uid=event.uid, original=key[1], error=str(error))

        self.checked = {k: v for k, v in self.checked.items() if k in overrides}
        if due:
            managed = {m.google_event_id for m in mappings.values()}
            extra = sum(parent in managed for parent, _ in actual)
            if extra:
                errors.append(f"{extra} additional managed Google instances need verification")
            log.info("mirror.audit", compared=len(expected), extra=extra, errors=len(errors))
            if not errors:
                self.next_audit = now + timedelta(minutes=10)
