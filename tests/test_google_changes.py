from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

from src.models.event import CalendarEvent, ChangeType
from src.sync.differ import detect_google_changes


def test_delta_cannot_delete_absent_mapped_meeting():
    repo = Mock()
    repo.get_all_mappings.return_value = [
        SimpleNamespace(yandex_uid=uid, google_event_id=uid, content_hash="old")
        for uid in ("changed", "unchanged", "cancelled")
    ]
    change = CalendarEvent(uid="changed", start=date(2031, 6, 20), end=date(2031, 6, 21))
    cancel = CalendarEvent(uid="cancelled", start=change.start, end=change.end, status="cancelled")
    result = detect_google_changes([change, cancel], repo)
    assert [(item.change_type, item.google_event_id) for item in result] == [
        (ChangeType.MODIFIED, None), (ChangeType.DELETED, "cancelled")
    ]
    assert detect_google_changes([], repo) == []
