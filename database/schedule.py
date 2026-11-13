from __future__ import annotations

from datetime import date, datetime

from database.connection import get_pool


async def save_lessons(
    *,
    kind: str,
    value: str,
    lessons: list,
    start: date | None = None,
    end: date | None = None,
) -> None:
    if not lessons and (start is None or end is None):
        raise ValueError("An empty replacement requires explicit week bounds")

    pool = get_pool()

    first_date = start if start is not None else min(x.date for x in lessons)
    last_date = end if end is not None else max(x.date for x in lessons)

    if first_date > last_date or any(not first_date <= x.date <= last_date for x in lessons):
        raise ValueError("Lessons outside replacement range")

    async with pool.acquire() as conn:
        async with conn.transaction():

            await conn.execute(
                """
                DELETE FROM schedule_lessons
                WHERE kind = $1
                  AND owner = $2
                  AND lesson_date BETWEEN $3 AND $4
                """,
                kind,
                value,
                first_date,
                last_date,
            )

            for lesson in lessons:
                teacher = getattr(
                    lesson,
                    "teacher",
                    None,
                )

                group_code = getattr(
                    lesson,
                    "group_code",
                    None,
                )

                start_time = datetime.strptime(
                    lesson.start,
                    "%H:%M",
                ).time()

                end_time = datetime.strptime(
                    lesson.end,
                    "%H:%M",
                ).time()

                await conn.execute(
                    """
                    INSERT INTO schedule_lessons (
                        kind,
                        owner,
                        lesson_date,
                        start_time,
                        end_time,
                        subject,
                        lesson_type,
                        teacher,
                        group_code,
                        room
                    )
                    VALUES (
                        $1, $2, $3, $4, $5,
                        $6, $7, $8, $9, $10
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    kind,
                    value,
                    lesson.date,
                    start_time,
                    end_time,
                    lesson.subject,
                    lesson.lesson_type,
                    teacher,
                    group_code,
                    lesson.room,
                )
