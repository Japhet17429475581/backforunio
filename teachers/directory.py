from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from config import REQUEST_TIMEOUT, TEACHER_PORTAL_URL


def merge_teacher_entries(entries: list[dict]) -> list[dict]:
    def parts(name: str) -> tuple[str, ...]:
        return tuple(re.findall(r"[а-яa-z-]+", name.casefold().replace("ё", "е")))

    unique: dict[tuple[str, ...], dict] = {}

    def add(key: tuple[str, ...], entry: dict) -> None:
        target = unique.setdefault(key, {"fio": entry["fio"], "schedule_fios": []})
        for fio in entry["schedule_fios"]:
            if fio not in target["schedule_fios"]:
                target["schedule_fios"].append(fio)

    for entry in entries:
        add(parts(entry["fio"]), entry)
    full_names = [key for key in unique if len(key) >= 2 and all(len(p) > 1 for p in key)]
    for key, entry in list(unique.items()):
        if len(key) < 2 or not any(len(p) == 1 for p in key[1:]):
            continue
        candidates = [other for other in full_names
                      if len(key) == len(other) and key[0] == other[0]
                      and all(a == b or (len(a) == 1 and b.startswith(a))
                              for a, b in zip(key[1:], other[1:]))]
        # Never guess between namesakes. Keep every individual/joint lookup key.
        if len(candidates) == 1:
            add(candidates[0], entry)
            del unique[key]
    return list(unique.values())


def parse_teacher_directory(content: str) -> list[dict]:
    """Read public schedule links, including jointly taught lessons.

    A person appears once in search, while schedule_fios retains every portal
    lookup key, so splitting a joint entry does not lose its shared lessons.
    """
    soup = BeautifulSoup(content, "html.parser")
    teachers: dict[str, dict] = {}
    source = urlparse(TEACHER_PORTAL_URL)
    for link in soup.select(".schedule-filter-list__item a[href]"):
        target = urlparse(urljoin(TEACHER_PORTAL_URL, link["href"]))
        if target.hostname != source.hostname or target.path != source.path:
            continue
        raw = " ".join(parse_qs(target.query).get("fio", [""])[0].split())
        if not raw or len(raw) > 255:
            continue
        for name in raw.split(","):
            name = " ".join(name.split())
            if not name:
                continue
            key = name.casefold().replace("ё", "е")
            entry = teachers.setdefault(key, {"fio": name, "schedule_fios": []})
            if raw not in entry["schedule_fios"]:
                entry["schedule_fios"].append(raw)
    if not teachers:
        raise ValueError("Портал не вернул справочник преподавателей.")
    return sorted(merge_teacher_entries(list(teachers.values())),
                  key=lambda item: item["fio"].casefold().replace("ё", "е"))


class TeacherDirectoryService:
    def __init__(self, *, ttl: int = 86400):
        self.ttl = ttl
        self._cached: dict | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def _fetch(self) -> str:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = await client.get(TEACHER_PORTAL_URL)
            response.raise_for_status()
            return response.text

    async def get(self) -> dict:
        if self._cached is not None and time.monotonic() < self._expires:
            return self._cached
        async with self._lock:
            if self._cached is not None and time.monotonic() < self._expires:
                return self._cached
            try:
                teachers = parse_teacher_directory(await self._fetch())
                self._cached = {
                    "success": True, "teachers": teachers, "stale": False,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                self._expires = time.monotonic() + self.ttl
            except (httpx.HTTPError, ValueError):
                if self._cached is None:
                    raise
                self._cached = {**self._cached, "stale": True}
                # Avoid hammering an unavailable portal on each screen opening.
                self._expires = time.monotonic() + 300
            return self._cached
