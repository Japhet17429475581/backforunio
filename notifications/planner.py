from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

FIRST_LESSON_OFFSET_MINUTES = 60
NEXT_LESSON_OFFSET_MINUTES = 10


def _parse_time(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _notification_id(target_date: date, start: str, subject: str, index: int) -> str:
    safe_subject = "".join(ch if ch.isalnum() else "-" for ch in subject.lower()).strip("-")
    return f"lesson-{target_date.isoformat()}-{start.replace(':', '')}-{index}-{safe_subject[:40]}"


def build_notification_plan(
    *,
    lessons: list[dict[str, Any]],
    target_date: date,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Build a deterministic plan for Flutter local notifications.

    First lesson: 60 minutes before.
    Every following lesson: 10 minutes before.

    The backend does not send push notifications. Flutter schedules these
    notifications locally in the operating system.
    """
    current = now or datetime.now()
    day_lessons = [
        item for item in lessons
        if str(item.get("date", target_date.isoformat())) == target_date.isoformat()
        and item.get("start")
    ]
    day_lessons.sort(key=lambda item: item["start"])

    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    type_labels = {
        "lecture": "лекция",
        "practice": "практика",
        "lab": "лабораторная",
        "seminar": "семинар",
        "consultation": "консультация",
        "exam": "экзамен",
        "credit": "зачёт",
    }

    for index, lesson in enumerate(day_lessons):
        start = str(lesson["start"])
        start_dt = datetime.combine(target_date, _parse_time(start))
        offset = FIRST_LESSON_OFFSET_MINUTES if index == 0 else NEXT_LESSON_OFFSET_MINUTES
        notify_at = start_dt - timedelta(minutes=offset)

        subject = str(lesson.get("subject") or "Пара")
        room = str(lesson.get("room") or "").strip()
        lesson_type = str(lesson.get("lesson_type") or "").strip()
        type_label = type_labels.get(lesson_type, lesson_type)

        body_parts = [subject]
        if type_label:
            body_parts.append(type_label)
        body_parts.append(f"в {start}")
        if room:
            body_parts.append(f"ауд. {room}")

        notification_id = _notification_id(target_date, start, subject, index)
        if notification_id in seen:
            continue
        seen.add(notification_id)

        result.append({
            "id": notification_id,
            "scheduled_at": notify_at.isoformat(timespec="minutes"),
            "minutes_before": offset,
            "title": "Пара скоро начнётся",
            "body": " · ".join(body_parts),
            "date": target_date.isoformat(),
            "lesson": {
                "start": start,
                "end": lesson.get("end"),
                "subject": subject,
                "lesson_type": lesson.get("lesson_type"),
                "room": room,
                "teacher": lesson.get("teacher"),
            },
            "action": {
                "type": "open_schedule",
                "date": target_date.isoformat(),
            },
            "past": notify_at <= current,
        })

    return result
