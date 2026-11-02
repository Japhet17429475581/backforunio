from __future__ import annotations

from datetime import datetime

from database.connection import get_pool


async def save_lessons(
    *,
    kind: str,
    value: str,
    lessons: list,
) -> None:
    if not lessons:
        return

    pool = get_pool()

    first_date = min(x.date for x in lessons)
    last_date = max(x.date for x in lessons)

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
