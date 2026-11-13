from __future__ import annotations
 
import asyncio
import hashlib
import os
import re
import weakref
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
 
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from config import APP_HOST, APP_PORT, CACHE_TTL, PORTAL_URL, TEACHER_PORTAL_URL, REQUEST_TIMEOUT
from database import (
    close_db,
    get_pool,
    init_db,
    save_lessons,
)
from redis_db import (
    close_redis,
    get_redis,
    init_redis,
    redis_delete,
    redis_get_json,
    redis_set_json,
    redis_try_lock,
    redis_unlock,
    RedisUnavailable,
)

from auth_api import router as auth_router
from news.router import router as news_router
from news.router import service as news_service
from config import NEWS_SYNC_INTERVAL
from notifications import build_notification_plan
from teachers.parser import TeacherScheduleParser
from groups import router as groups_router, directory as group_directory
from privacy import retention_loop
from server_logging import SafeRequestLogMiddleware, configure_private_logging, log_event
from diagnostics import router as diagnostics_router
from assistant.router import router as assistant_router
from assistant.service import Assistant
from assistant.startup import prepare_corpus
from web_portal import router as web_portal_router, body_guard
from web_push import router as web_push_router, loop as web_push_loop
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
configure_private_logging()

async def _news_sync_loop():
    # Первая синхронизация после старта, затем один раз в сутки.
    await asyncio.sleep(5)
    while True:
        try:
            result = await news_service.sync_news()
            if result.get('skipped'):
                log_event('news_sync_waiting_for_lock')
                await asyncio.sleep(30)
                continue
            log_event('news_sync_complete')
        except Exception as exc:
            log_event('news_sync_failed', level=40, error=exc)
            await asyncio.sleep(60)
            continue
        await asyncio.sleep(NEWS_SYNC_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Every acquired resource has a matching cleanup, including partial startup.
    # ExitStack runs the remaining callbacks even if one cleanup fails.
    async with AsyncExitStack() as resources:
        resources.push_async_callback(close_db)
        resources.push_async_callback(close_redis)
        resources.push_async_callback(group_directory.close)
        try:
            log_event('postgres_starting')
            await init_db()
            log_event('postgres_ready')
            global _schedule_client
            _schedule_client = httpx.AsyncClient(timeout=httpx.Timeout(3, connect=2), follow_redirects=True)
            resources.push_async_callback(_schedule_client.aclose)
            await prepare_corpus()
            log_event('redis_starting')
            await init_redis()
            log_event('redis_ready')
            app.state.assistant = Assistant()
            resources.push_async_callback(app.state.assistant.close)
            for worker in (_news_sync_loop, retention_loop):
                resources.push_async_callback(
                    _stop_background_task, asyncio.create_task(worker()))
            resources.push_async_callback(
                _stop_background_task, asyncio.create_task(web_push_loop(app)))
        except Exception as error:
            log_event('startup_failed', level=40, error=error)
            raise
        yield


async def _stop_background_task(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as error:
        log_event('background_task_failed', level=40, error=error)


app = FastAPI(
    title="UNIO API",
    version="1.2.0",
    description="Backend платформы UNIO для студентов",
    lifespan=lifespan,
)
 
@app.exception_handler(RequestValidationError)
async def private_validation_error(request, exc):
    # Pydantic errors can echo input bodies (including mistakenly sent credentials).
    return JSONResponse(status_code=422, content={"detail": {"code": "invalid_request"}})


@app.exception_handler(Exception)
async def private_server_error(request, exc):
    return JSONResponse(status_code=500, content={"detail": {"code": "internal_error"}},
                        headers={'X-Request-Id': request.scope.get('unio.request_id', '')})


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware('http')
async def schedule_request_limit(request, call_next):
    if request.url.path in ('/api/v1/schedule', '/api/v1/teachers/schedule'):
        from rate_limit import enforce_rate_limit
        force = request.query_params.get('force_refresh', '').lower() in ('true', '1', 'yes', 'on')
        host = request.client.host if request.client else 'unknown'
        try:
            await enforce_rate_limit(key=f'schedule:ip:{host}:{force}',
                                     limit=12 if force else 120, window_seconds=60)
        except HTTPException as error:
            return JSONResponse({'detail': error.detail}, status_code=error.status_code, headers=error.headers)
    return await call_next(request)


# Personal portal operations run directly on the device; legacy routes are unmounted.
app.include_router(auth_router)
app.include_router(news_router)
app.include_router(groups_router)
app.include_router(web_portal_router)
app.include_router(web_push_router)
app.include_router(diagnostics_router)
app.include_router(assistant_router)
app.middleware('http')(body_guard)
app.add_middleware(SafeRequestLogMiddleware)
# ============================================================
# PARSERS
# ============================================================
 
DAY_RE = re.compile(
    r"^(ПН|ВТ|СР|ЧТ|ПТ|СБ|ВС)\s+(\d{1,2})\.(\d{1,2})$",
    re.I,
)
TIME_RE = re.compile(
    r"(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})"
)
TYPE_RE = re.compile(
    r"^(лек|пр|лпз|лаб|сем|конс|экз|зач|диф\.?\s*зач)\s+",
    re.I,
)
 
TYPE_MAP = {
    "лек": "lecture",
    "пр": "practice",
    "лпз": "practice",
    "лаб": "lab",
    "сем": "seminar",
    "конс": "consultation",
    "экз": "exam",
    "зач": "credit",
    "диф.зач": "credit",
}
 
 
@dataclass(slots=True, frozen=True)
class StudentLesson:
    date: date
    start: str
    end: str
    subject: str
    lesson_type: str | None
    teacher: str
    room: str
 
 
@dataclass(slots=True, frozen=True)
class TeacherLesson:
    date: date
    start: str
    end: str
    subject: str
    lesson_type: str | None
    group_code: str
    room: str
 
 
class StudentScheduleParser:
    def parse(self, content: str) -> list[StudentLesson]:
        soup = BeautifulSoup(content, "html.parser")
        table = self._find_schedule_table(soup)
        if table is None:
            raise ValueError("Таблица расписания группы не найдена")
 
        lessons: list[StudentLesson] = []
        current_date: date | None = None
 
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
 
            values = [self.clean_text(c.get_text(" ", strip=True)) for c in cells]
            values = [v for v in values if v]
 
            if not values:
                continue
 
            detected = self._parse_day(values)
            if detected:
                current_date = detected
                continue
 
            if self._looks_like_header(values):
                continue
            if current_date is None:
                if TIME_RE.search(" ".join(values)):
                    raise ValueError("Schedule row has no recognized date")
                continue
 
            lesson = self._parse_lesson(values, current_date)
            if lesson:
                lessons.append(lesson)
            elif TIME_RE.search(" ".join(values)):
                raise ValueError("Schedule row could not be parsed")
 
        return self._deduplicate(lessons)
 
    def _find_schedule_table(self, soup: BeautifulSoup):
        for table in soup.find_all("table"):
            text = self.clean_text(table.get_text(" ", strip=True)).lower()
            required = ("дата", "время", "предмет", "преподаватель", "аудитория")
            if all(item in text for item in required):
                return table
 
        for table in soup.find_all("table"):
            text = self.clean_text(table.get_text(" ", strip=True))
            if re.search(
                r"(?:ПН|ВТ|СР|ЧТ|ПТ|СБ|ВС)\s+\d{1,2}\.\d{1,2}",
                text,
                re.I,
            ) and TIME_RE.search(text):
                return table
 
        return None
 
    @staticmethod
    def _parse_day(values: list[str]) -> date | None:
        for value in values:
            match = DAY_RE.match(value)
            if match:
                return resolve_year(
                    int(match.group(2)),
                    int(match.group(3)),
                )
        return None
 
    def _parse_lesson(
        self,
        values: list[str],
        lesson_date: date,
    ) -> StudentLesson | None:
        time_index = None
        match = None
 
        for index, value in enumerate(values):
            match = TIME_RE.search(value)
            if match:
                time_index = index
                break
 
        if match is None or time_index is None:
            return None
 
        start, end = match.group(1), match.group(2)
        after = values[time_index + 1:]
 
        if not after:
            return None
 
        subject, lesson_type = split_type(after[0])
        if not subject or DAY_RE.match(subject):
            return None
 
        return StudentLesson(
            date=lesson_date,
            start=start,
            end=end,
            subject=subject,
            lesson_type=lesson_type,
            teacher=after[1] if len(after) > 1 else "",
            room=after[2] if len(after) > 2 else "",
        )
 
    @staticmethod
    def _looks_like_header(values: list[str]) -> bool:
        joined = " ".join(values).lower()
        return "дата" in joined and "время" in joined and "предмет" in joined
 
    @staticmethod
    def clean_text(value: str) -> str:
        for char in ("\u200b", "\u200c", "\u200d", "\ufeff", "\xa0"):
            value = value.replace(char, " ")
        return re.sub(r"\s+", " ", value).strip()
 
    @staticmethod
    def _deduplicate(lessons: list[StudentLesson]) -> list[StudentLesson]:
        unique = {
            (
                x.date,
                x.start,
                x.end,
                x.subject,
                x.lesson_type,
                x.teacher,
                x.room,
            ): x
            for x in lessons
        }
 
        return sorted(
            unique.values(),
            key=lambda x: (
                x.date,
                datetime.strptime(x.start, "%H:%M").time(),
                x.subject,
            ),
        )
 
 
# ============================================================
# HELPERS / CACHE
# ============================================================
 
 
def resolve_year(day: int, month: int) -> date:
    today = date.today()
    candidates = [
        date(y, month, day)
        for y in (today.year - 1, today.year, today.year + 1)
    ]
    return min(candidates, key=lambda item: abs((item - today).days))
 
 
def split_type(value: str) -> tuple[str, str | None]:
    match = TYPE_RE.match(value)
    if not match:
        return value.strip(), None
 
    raw = re.sub(r"\s+", "", match.group(1).lower())
    raw = raw.replace("ё", "е")
 
    return value[match.end():].strip(), TYPE_MAP.get(raw)
 
 
def week_bounds(target: date) -> tuple[date, date]:
    start = target - timedelta(days=target.weekday())
    return start, start + timedelta(days=6)
 
 
@dataclass(slots=True)
class CacheEntry:
    created_at: datetime
    lessons: list
 
 
student_cache: dict[tuple[str, int, int], CacheEntry] = {}
teacher_cache: dict[tuple[str, int, int], CacheEntry] = {}
_schedule_locks = weakref.WeakValueDictionary()
_portal_slots = asyncio.Semaphore(3)
_schedule_client = None
SCHEDULE_DEADLINE = 8


def _schedule_lock(key):
    lock = _schedule_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _schedule_locks[key] = lock
    return lock
MAX_LOCAL_CACHE_ENTRIES = 256


def _put_local_cache(cache: dict, key, entry: CacheEntry) -> None:
    cache[key] = entry
    if len(cache) <= MAX_LOCAL_CACHE_ENTRIES:
        return
    oldest_key = min(cache, key=lambda item: cache[item].created_at)
    cache.pop(oldest_key, None)
 
student_parser = StudentScheduleParser()
teacher_parser = TeacherScheduleParser()
 
 
async def fetch_html(
    *,
    kind: str,
    value: str,
    week: int,
    year: int,
) -> str:
    params = {
        "week": week,
        "year": year,
    }
 
    if kind == "group":
        params["group"] = value
    else:
        params["fio"] = value
 
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/140 Safari/537.36"
        )
    }
 
    async def read(client):
        last_error = None
        for attempt in range(2):
            try:
                response = await client.get(PORTAL_URL if kind == 'group' else TEACHER_PORTAL_URL,
                                            params=params, headers=headers)
                response.raise_for_status()
                return response.text
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as error:
                last_error = error
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500:
                    break
                if attempt == 0:
                    await asyncio.sleep(0.15)
        raise last_error
    async with _portal_slots:
        if _schedule_client is not None and not _schedule_client.is_closed:
            return await read(_schedule_client)
        async with httpx.AsyncClient(timeout=httpx.Timeout(3, connect=2), follow_redirects=True) as client:
            return await read(client)

 
async def _decode_schedule_payload(kind: str, payload) -> tuple[list, datetime | None]:
    # Поддерживаем и старый формат Redis (просто list), и новый объект.
    if isinstance(payload, dict):
        raw_lessons = payload.get("lessons", [])
        cached_at_raw = payload.get("cached_at")
        try:
            cached_at = datetime.fromisoformat(cached_at_raw) if cached_at_raw else None
        except (TypeError, ValueError):
            cached_at = None
    else:
        raw_lessons = payload or []
        cached_at = None

    lessons = []
    for item in raw_lessons:
        if kind == "group":
            lessons.append(StudentLesson(
                date=date.fromisoformat(item["date"]),
                start=item["start"], end=item["end"],
                subject=item["subject"], lesson_type=item.get("lesson_type"),
                teacher=item.get("teacher", ""), room=item.get("room", ""),
            ))
        else:
            lessons.append(TeacherLesson(
                date=date.fromisoformat(item["date"]),
                start=item["start"], end=item["end"],
                subject=item["subject"], lesson_type=item.get("lesson_type"),
                group_code=item.get("group", ""), room=item.get("room", ""),
            ))
    return lessons, cached_at


async def _read_schedule_cache(key):
    try:
        async with asyncio.timeout(0.35):
            return await redis_get_json(key)
    except TimeoutError:
        return None


async def _get_lessons(
    *, kind: str, value: str, target: date, force_refresh: bool
) -> tuple[list, str, datetime | None]:
    iso = target.isocalendar()
    normalized = value.strip()
    value_hash = hashlib.sha256(normalized.casefold().encode("utf-8")).hexdigest()[:24]
    if force_refresh:
        from rate_limit import enforce_rate_limit
        await enforce_rate_limit(key=f'schedule:refresh:{kind}:{value_hash}', limit=4, window_seconds=60)
    cache_key = f"schedule:{kind}:{value_hash}:{iso.year}:{iso.week}"
    lock_key = f"lock:{cache_key}"
    local_key = (normalized, iso.year, iso.week)
    local_cache = student_cache if kind == "group" else teacher_cache

    if not force_refresh:
        cached = local_cache.get(local_key)
        if cached and (datetime.now() - cached.created_at).total_seconds() < CACHE_TTL:
            return cached.lessons, "memory", cached.created_at

        payload = await _read_schedule_cache(cache_key)
        if payload is not None:
            lessons, cached_at = await _decode_schedule_payload(kind, payload)
            _put_local_cache(local_cache, local_key, CacheEntry(cached_at or datetime.now(), lessons))
            return lessons, "redis", cached_at

    # Локальный lock + Redis lock защищают от одновременного похода на портал
    # даже при нескольких Uvicorn workers.
    if force_refresh:
        payload = await _read_schedule_cache(cache_key)
        if payload is not None:
            lessons, cached_at = await _decode_schedule_payload(kind, payload)
            if cached_at and 0 <= (datetime.now() - cached_at).total_seconds() < 30:
                return lessons, 'redis', cached_at
    async with _schedule_lock(cache_key):
        if not force_refresh:
            cached = local_cache.get(local_key)
            if cached and (datetime.now() - cached.created_at).total_seconds() < CACHE_TTL:
                return cached.lessons, "memory", cached.created_at
            payload = await _read_schedule_cache(cache_key)
            if payload is not None:
                lessons, cached_at = await _decode_schedule_payload(kind, payload)
                _put_local_cache(local_cache, local_key, CacheEntry(cached_at or datetime.now(), lessons))
                return lessons, "redis", cached_at

        cached = local_cache.get(local_key)
        if force_refresh and cached and (datetime.now() - cached.created_at).total_seconds() < 30:
            return cached.lessons, 'memory', cached.created_at
        try:
            async with asyncio.timeout(0.35):
                lock_token = await redis_try_lock(lock_key, ttl=15, strict=True)
        except (RedisUnavailable, TimeoutError):
            # Public timetable can operate with process-local deduplication.
            lock_token = 'local-only'
        if lock_token is None:
            # Другой worker сейчас обновляет. Ждём его результат, чтобы не ударить портал повторно.
            for _ in range(18):
                await asyncio.sleep(0.4)
                payload = await _read_schedule_cache(cache_key)
                if payload is not None:
                    lessons, cached_at = await _decode_schedule_payload(kind, payload)
                    _put_local_cache(local_cache, local_key, CacheEntry(cached_at or datetime.now(), lessons))
                    return lessons, "redis", cached_at
            raise httpx.HTTPError("Не удалось дождаться обновления кэша расписания")

        try:
            html = await fetch_html(kind=kind, value=normalized, week=iso.week, year=iso.year)
            lessons = student_parser.parse(html) if kind == "group" else teacher_parser.parse(html)
            cached_at = datetime.now()
            _put_local_cache(local_cache, local_key, CacheEntry(cached_at, lessons))

            try:
                await save_lessons(kind=kind, value=normalized, lessons=lessons, start=week_bounds(target)[0], end=week_bounds(target)[1])
            except Exception as exc:
                log_event('schedule_save_failed', level=30, error=exc)

            payload = []
            for lesson in lessons:
                item = {
                    "date": lesson.date.isoformat(),
                    "start": lesson.start,
                    "end": lesson.end,
                    "subject": lesson.subject,
                    "lesson_type": lesson.lesson_type,
                    "room": lesson.room,
                }
                item["teacher" if kind == "group" else "group"] = (
                    lesson.teacher if kind == "group" else lesson.group_code
                )
                payload.append(item)

            try:
                async with asyncio.timeout(0.35):
                    await redis_set_json(cache_key, {"lessons": payload, "cached_at": cached_at.isoformat()}, CACHE_TTL)
            except TimeoutError:
                pass
            return lessons, "portal", cached_at
        finally:
            if lock_token and lock_token != 'local-only':
                try:
                    async with asyncio.timeout(0.35):
                        await redis_unlock(lock_key, lock_token)
                except TimeoutError:
                    pass

async def get_lessons(*, kind, value, target, force_refresh):
    if kind not in ('group', 'teacher') or not value.strip() or len(value) > 255:
        raise HTTPException(422, detail={'code': 'invalid_schedule_owner'})
    if abs((target - date.today()).days) > 366:
        raise HTTPException(422, detail={'code': 'invalid_schedule_date'})
    try:
        async with asyncio.timeout(SCHEDULE_DEADLINE):
            return await _get_lessons(kind=kind, value=value, target=target, force_refresh=force_refresh)
    except (TimeoutError, httpx.HTTPError):
        iso = target.isocalendar()
        cached = (student_cache if kind == 'group' else teacher_cache).get((value.strip(), iso.year, iso.week))
        if cached and (datetime.now() - cached.created_at).total_seconds() < 7 * 86400:
            return cached.lessons, 'stale', cached.created_at
        raise httpx.HTTPError('Schedule temporarily unavailable') from None


from teachers.router import router as teacher_router
# Bind the existing loader without importing main from the router.
app.state.get_lessons = get_lessons
app.include_router(teacher_router)

# API
# ============================================================
 
 
@app.get("/api/v1/schedule")
async def schedule(
    group: str | None = Query(
        default=None,
        description="Код группы, например ЕН-ФББ-111",
    ),
    teacher: str | None = Query(
        default=None,
        description="ФИО преподавателя, например Гуляева Ирина Ивановна",
    ),
    target_date: date | None = Query(
        default=None,
        alias="date",
        description="Дата в формате YYYY-MM-DD",
    ),
    week: bool = Query(
        default=False,
        description="Вернуть всю неделю вместо одного дня",
    ),
    force_refresh: bool = Query(default=False),
):
    if bool(group) == bool(teacher):
        raise HTTPException(
            status_code=400,
            detail="Укажите ровно один параметр: group или teacher",
        )
 
    value = (group or teacher or "").strip()
    kind = "group" if group else "teacher"
    target = target_date or date.today()
 
    try:
        lessons, cache_source, cache_updated_at = await get_lessons(
            kind=kind,
            value=value,
            target=target,
            force_refresh=force_refresh,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="Не удалось получить расписание с портала",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="Request unavailable",
        ) from exc
 
    if week:
        start, end = week_bounds(target)
        selected = [x for x in lessons if start <= x.date <= end]
    else:
        selected = [x for x in lessons if x.date == target]
 
    result = []
 
    for lesson in selected:
        item = {
            "date": lesson.date.isoformat(),
            "start": lesson.start,
            "end": lesson.end,
            "subject": lesson.subject,
            "lesson_type": lesson.lesson_type,
            "room": lesson.room,
        }
 
        if kind == "group":
            item["teacher"] = lesson.teacher
        else:
            item["group"] = lesson.group_code
 
        result.append(item)
 
    return {
        "success": True,
        "type": kind,
        "query": value,
        "date": target.isoformat(),
        "week": week,
        "count": len(result),
        "lessons": result,
        "cache": {
            "source": cache_source,
            "updated_at": cache_updated_at.isoformat() if cache_updated_at else None,
        },
    }

@app.get("/api/v1/notifications/plan")
async def notification_plan(
    group: str | None = Query(
        default=None,
        description="Код группы, например ЕН-ФББ-111",
    ),
    target_date: date | None = Query(
        default=None,
        alias="date",
        description="Дата в формате YYYY-MM-DD",
    ),
    include_past: bool = Query(
        default=False,
        description="Включить уведомления, время которых уже прошло",
    ),
):
    """Вернуть план локальных уведомлений для расписания группы.

    Backend не отправляет FCM/APNs. Flutter получает этот план и создаёт
    локальные уведомления средствами ОС устройства.
    """
    if not group or not group.strip():
        raise HTTPException(status_code=400, detail="Укажите group")

    target = target_date or date.today()
    value = group.strip()

    try:
        lessons, cache_source, cache_updated_at = await get_lessons(
            kind="group",
            value=value,
            target=target,
            force_refresh=False,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось получить расписание с портала: {exc}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Request unavailable") from exc

    schedule_items = [
        {
            "date": lesson.date.isoformat(),
            "start": lesson.start,
            "end": lesson.end,
            "subject": lesson.subject,
            "lesson_type": lesson.lesson_type,
            "teacher": lesson.teacher,
            "room": lesson.room,
        }
        for lesson in lessons
        if lesson.date == target
    ]

    plan = build_notification_plan(
        lessons=schedule_items,
        target_date=target,
        now=datetime.now(),
    )

    if not include_past:
        plan = [item for item in plan if not item["past"]]

    return {
        "success": True,
        "date": target.isoformat(),
        "group": value,
        "count": len(plan),
        "notifications": plan,
        "rules": {
            "first_lesson_minutes_before": 60,
            "next_lessons_minutes_before": 10,
        },
        "action": {
            "type": "open_schedule",
            "date": target.isoformat(),
        },
        "cache": {
            "source": cache_source,
            "updated_at": cache_updated_at.isoformat()
            if cache_updated_at else None,
        },
    }

 
 
@app.get("/health")
async def health():
    postgres = "error"
    redis_status = "error"
    checks: dict[str, str] = {}

    try:
        async with get_pool().acquire() as conn:
            await conn.execute("SELECT 1")
        postgres = "ok"
    except Exception as exc:
        checks["postgres_error"] = "dependency_unavailable"

    try:
        await get_redis().ping()
        redis_status = "ok"
    except Exception as exc:
        checks["redis_error"] = "dependency_unavailable"

    status = "ok" if postgres == "ok" and redis_status == "ok" else "degraded"
    payload = {
        "status": status,
        "postgresql": postgres,
        "redis": redis_status,
    }
    from assistant.startup import corpus_status
    from privacy import retention_status
    payload['assistant'] = dict(corpus_status)
    payload['retention'] = dict(retention_status)
    payload.update(checks)

    if status != "ok":
        raise HTTPException(status_code=503, detail=payload)
    return payload


@app.get('/health/live')
async def liveness():
    return {'status': 'ok'}


@app.get('/health/ready')
async def readiness():
    payload = await health()
    from assistant import config as ai_config
    if ai_config.ENABLED and payload['assistant']['status'] != 'ready':
        raise HTTPException(503, detail=payload)
    return payload


@app.get("/health/portal")
async def portal_health():
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(5.0),
            follow_redirects=True,
            headers={"User-Agent": "UNIO-HealthCheck/1.0"},
        ) as client:
            response = await client.get(PORTAL_URL)
            response.raise_for_status()
        return {"status": "ok", "portal": "ok", "status_code": response.status_code}
    except Exception as exc:
        raise HTTPException(503, {"status": "degraded", "portal": "error", "error": "dependency_unavailable"}) from exc


@app.get("/")
async def root():
    return {
        "name": "VGSPU Schedule API",
        "version": "1.0.0",
        "docs": "/docs",
        "endpoint": "/api/v1/schedule",
    }
 
 
if __name__ == "__main__":
    import uvicorn
 
    uvicorn.run(
        app,
        host=APP_HOST,
        port=APP_PORT,
        reload=False,
        access_log=False,
        log_config=None,
    )
