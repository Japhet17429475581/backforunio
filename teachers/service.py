from __future__ import annotations

from datetime import date, datetime
from typing import Any

from database import get_pool


def normalize_fio(fio: str) -> str:
    return " ".join(fio.strip().split())


async def find_free_rooms(
    *,
    target: date,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Ищет свободные аудитории среди уже известных UNIO расписаний.

    Список аудиторий не захардкожен: каталог строится автоматически из
    schedule_lessons. Занятость проверяется по пересечению интервалов.
    """
    try:
        start_time = datetime.strptime(start, "%H:%M").time()
        end_time = datetime.strptime(end, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("Время должно быть в формате HH:MM") from exc

    if start_time >= end_time:
        raise ValueError("start должен быть раньше end")

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT trim(room) AS room
            FROM schedule_lessons
            WHERE lesson_date = $1
              AND room IS NOT NULL
              AND trim(room) <> ''
            ORDER BY room
            """,
            target,
        )
        catalog = [row["room"] for row in rows]

        occupied_rows = await conn.fetch(
            """
            SELECT DISTINCT trim(room) AS room
            FROM schedule_lessons
            WHERE lesson_date = $1
              AND room IS NOT NULL
              AND trim(room) <> ''
              AND start_time < $3
              AND end_time > $2
            ORDER BY room
            """,
            target,
            start_time,
            end_time,
        )

    occupied = {row["room"] for row in occupied_rows}
    free = [room for room in catalog if room not in occupied]

    return {
        "rooms": free,
        "occupied_rooms_count": len(occupied),
        "known_rooms_count": len(catalog),
        "coverage": "cached_schedule",
    }
