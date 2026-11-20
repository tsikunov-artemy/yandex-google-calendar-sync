from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import caldav
import pytest
from icalendar import Calendar

from src.models.event import CalendarEvent, EventStatus
from src.providers.google import GoogleProvider
from src.providers.yandex import YandexProvider
from src.sync.differ import detect_yandex_changes
from src.sync.mirror import GoogleMirror, MOSCOW, difference, instant


NOW = datetime(2031, 6, 20, 12, tzinfo=MOSCOW)
OLD = NOW - timedelta(days=1)
NEW = NOW + timedelta(hours=2)


def event(**fields):
    return CalendarEvent(uid="series", summary="Weekly", start=NEW, end=NEW+timedelta(hours=1), **fields)


def test_all_components_and_conference():
    raw = SimpleNamespace(data="""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series
DTSTART:20310619T100000Z
RRULE:FREQ=WEEKLY
END:VEVENT
BEGIN:VEVENT
UID:series
RECURRENCE-ID:20310619T100000Z
DTSTART:20310620T110000Z
ATTENDEE:mailto:a@example.org
X-TELEMOST-CONFERENCE:https://example.org/call
END:VEVENT
END:VCALENDAR""", url=None)
    provider = YandexProvider(None)
    provider.calendar = MagicMock()
    provider.calendar.search.return_value = [raw]
    master, moved = provider.read_components(OLD, NEW, expand=False)
    assert master.is_recurring and not master.recurrence_id
    assert moved.recurrence_id == "2031-06-19T10:00:00+00:00"
    assert moved.attendees == ["a@example.org"]
    assert moved.description == "https://example.org/call"
    raw.data = "unreadable"
    with pytest.raises(ValueError):
        provider.read_components(OLD, NEW, expand=False)


def test_cancelled_without_start():
    component = Calendar.from_ical("BEGIN:VEVENT\r\nUID:s\r\nRECURRENCE-ID;VALUE=DATE:20310620\r\n"
                                  "STATUS:CANCELLED\r\nEND:VEVENT")
    parsed = YandexProvider(None)._parse_component(component, SimpleNamespace(url=None))
    assert parsed.recurrence_id == "2031-06-20"
    assert parsed.status == EventStatus.CANCELLED
    assert difference({"status": "cancelled"}, {"status": "confirmed"}) == {"status": "cancelled"}


def test_only_explicit_not_found_confirms_deletion():
    provider = YandexProvider(None)
    provider.calendar = MagicMock()
    assert not provider.confirmed_absent("uid", None)
    provider.calendar.event_by_uid.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        provider.confirmed_absent("uid", None)
    provider.calendar.event_by_uid.side_effect = caldav.error.NotFoundError("missing")
    assert provider.confirmed_absent("uid", None)


def test_orphan_override_cannot_delete_series():
    repo = MagicMock()
    repo.get_all_mappings.return_value = [SimpleNamespace(yandex_uid="series", google_event_id="google")]
    assert detect_yandex_changes([event(recurrence_id=OLD.isoformat())], repo) == []


@pytest.mark.parametrize("cancelled", [False, True])
def test_existing_instance_updated_once_and_audit_repairs_drift(cancelled):
    source, google, repo = MagicMock(), GoogleProvider(SimpleNamespace(google_calendar_id="mirror")), MagicMock()
    mirror = GoogleMirror(source, google, repo)
    occurrence = event(recurrence_id=OLD.isoformat(), status="cancelled" if cancelled else "confirmed")
    master = event(rrule="FREQ=WEEKLY")
    item = {"id": "same-google-id", "etag": "v1", "status": "confirmed", "summary": "Weekly",
            "start": {"dateTime": OLD.isoformat()}, "end": {"dateTime": (OLD+timedelta(hours=1)).isoformat()}}
    repo.get_all_mappings.return_value = [SimpleNamespace(yandex_uid="series", google_event_id="parent")]
    source.read_components.return_value = [] if cancelled else [occurrence]
    mirror.list_instances = lambda *_: {} if cancelled else {("parent", instant(OLD)): item}
    mirror.lookup = MagicMock(return_value=item)
    writes = []

    def write(current, patch):
        writes.append((current["id"], patch.copy()))
        current.update(patch)

    mirror.patch_item = write
    stats, errors = {"created": 0, "updated": 0}, []
    for minute in (0, 1, 11):
        mirror.sync([master, occurrence], NOW+timedelta(minutes=minute), OLD, NEW, stats, errors)
    assert errors == []
    assert len(writes) == 1 and writes[0][0] == "same-google-id"
    assert item["status"] == occurrence.status.value
    if not cancelled:
        item["description"] = "wrong link"
        mirror.sync([master, occurrence], NOW+timedelta(minutes=22), OLD, NEW, stats, errors)
        assert item["description"] == "" and stats["updated"] == 2


def test_retry_after_write_failure():
    source, google, repo = MagicMock(), GoogleProvider(SimpleNamespace(google_calendar_id="mirror")), MagicMock()
    mirror = GoogleMirror(source, google, repo)
    occurrence = event(recurrence_id=OLD.isoformat())
    repo.get_all_mappings.return_value = [SimpleNamespace(yandex_uid="series", google_event_id="g")]
    source.read_components.return_value = [occurrence]
    mirror.list_instances = lambda *_: {}
    mirror.lookup = MagicMock(return_value={"id":"id", "etag":"v", "status":"confirmed"})
    mirror.patch_item = MagicMock(side_effect=[TimeoutError("busy"), None])
    errors = []
    mirror.sync([occurrence], NOW, OLD, NEW, {"updated":0}, errors)
    assert errors and not mirror.checked
    errors = []
    mirror.sync([occurrence], NOW+timedelta(minutes=1), OLD, NEW, {"updated":0}, errors)
    assert not errors and mirror.checked
