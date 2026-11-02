from __future__ import annotations

from datetime import date, timedelta

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .service import find_free_rooms, normalize_fio
from .directory import TeacherDirectoryService

router = APIRouter(prefix="/api/v1/teachers", tags=["teachers"])
directory_service = TeacherDirectoryService()


@router.get("/directory")
async def teacher_directory():
    try:
        return await directory_service.get()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "Не удалось загрузить справочник преподавателей. Попробуйте позже.") from exc


class TeacherLoginRequest(BaseModel):
    fio: str = Field(min_length=3, max_length=255)


@router.post("/login")
async def teacher_login(data: TeacherLoginRequest):
    """Тестовый вход без проверки личности/пароля.

    В production эту схему заменить на реальную авторизацию преподавателя.
    """
    fio = normalize_fio(data.fio)
    if len(fio) < 3:
        raise HTTPException(400, "Укажите корректное ФИО")

    return {
        "success": True,
        "mode": "test",
        "verified": False,
        "teacher": {
            "fio": fio,
        },
    }


@router.get("/schedule")
async def teacher_schedule(
    request: Request,
    fio: str = Query(..., min_length=3, max_length=255),
    target_date: date | None = Query(default=None, alias="date"),
    week: bool = Query(default=False),
    force_refresh: bool = Query(default=False),
):
    target = target_date or date.today()
    normalized = normalize_fio(fio)

    try:
        lessons, cache_source, cached_at = await request.app.state.get_lessons(
            kind="teacher",
            value=normalized,
            target=target,
            force_refresh=force_refresh,
        )
    except ValueError as exc:
        raise HTTPException(502, "Request unavailable") from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(502, "Не удалось получить расписание преподавателя") from exc

    if week:
        start = target - timedelta(days=target.weekday())
        end = start + timedelta(days=6)
        selected = [lesson for lesson in lessons if start <= lesson.date <= end]
    else:
        selected = [lesson for lesson in lessons if lesson.date == target]

    groups = list(dict.fromkeys(lesson.group_code for lesson in selected if lesson.group_code))
    preview = groups[:3]

    return {
        "success": True,
        "teacher": normalized,
        "date": target.isoformat(),
        "week": week,
        "count": len(selected),
        "groups": {
            "preview": preview,
            "total": len(groups),
            "has_more": len(groups) > 3,
        },
        "lessons": [
            {
                "date": lesson.date.isoformat(),
                "start": lesson.start,
                "end": lesson.end,
                "subject": lesson.subject,
                "lesson_type": lesson.lesson_type,
                "group": lesson.group_code,
                "room": lesson.room,
            }
            for lesson in selected
        ],
        "cache": {
            "source": cache_source,
            "updated_at": cached_at.isoformat() if cached_at else None,
        },
    }


@router.get("/free-rooms")
async def teacher_free_rooms(
    target_date: date | None = Query(default=None, alias="date"),
    start: str = Query(..., pattern=r"^\d{2}:\d{2}$"),
    end: str = Query(..., pattern=r"^\d{2}:\d{2}$"),
):
    target = target_date or date.today()
    try:
        result = await find_free_rooms(target=target, start=start, end=end)
    except ValueError as exc:
        raise HTTPException(400, "Request unavailable") from exc
    except RuntimeError as exc:
        raise HTTPException(503, "Request unavailable") from exc

    return {
        "success": True,
        "date": target.isoformat(),
        "interval": {"start": start, "end": end},
        **result,
        "note": "Свободные аудитории определяются по расписаниям, которые уже известны UNIO.",
    }
