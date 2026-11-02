from datetime import date, datetime

from notifications.planner import build_notification_plan


def test_notification_offsets():
    target = date(2026, 10, 4)
    lessons = [
        {"date": "2026-10-04", "start": "09:00", "end": "10:30", "subject": "Математика", "room": "301"},
        {"date": "2026-10-04", "start": "11:00", "end": "12:30", "subject": "История", "room": "205"},
        {"date": "2026-10-04", "start": "13:00", "end": "14:30", "subject": "Информатика", "room": "12"},
    ]
    plan = build_notification_plan(
        lessons=lessons,
        target_date=target,
        now=datetime(2026, 10, 3, 12, 0),
    )
    assert [x["scheduled_at"] for x in plan] == [
        "2026-10-04T08:00",
        "2026-10-04T10:50",
        "2026-10-04T12:50",
    ]
    assert [x["minutes_before"] for x in plan] == [60, 10, 10]
