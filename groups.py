"""Public group catalogue; membership is independent of timetable contents."""
from __future__ import annotations

import asyncio
import time
import json
from datetime import datetime, timezone, timedelta
from database.connection import get_pool
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException

from config import PORTAL_URL, REQUEST_TIMEOUT


def group_key(value: str) -> str:
    return ''.join(c for c in value.upper() if c.isalnum())


def directory_links(content: str, base: str) -> tuple[set[str], set[str]]:
    soup = BeautifulSoup(content, 'html.parser')
    if not soup.select_one('.schedule-filter-list, .schedule-filter-row'):
        raise ValueError('Unrecognized group directory')
    source = urlsplit(base)
    groups, branches = set(), set()
    for link in soup.select('.schedule-filter-list__item a[href], .schedule-filter-row__item a[href]'):
        target = urlsplit(urljoin(base, link['href']))
        if (target.scheme, target.netloc, target.path) != (source.scheme, source.netloc, source.path):
            continue
        query = parse_qs(target.query)
        if set(query) == {'group'}:
            group = query['group'][0].strip()
            if group and len(group) <= 100:
                groups.add(group)
        elif query and set(query) <= {'code', 'level', 'form'}:
            branches.add(urlunsplit((source.scheme, source.netloc, source.path,
                                    urlencode(sorted((key, values[0]) for key, values in query.items())), '')))
    return groups, branches


class GroupDirectory:
    def __init__(self, ttl: int = 21600):
        self.ttl = ttl
        self._groups: list[str] | None = None
        self._expires = 0.0
        self._retry_after = 0.0
        self._lock = asyncio.Lock()
        self._saved_at = None
        self.stale = False
        self._refresh_task = None

    async def _fetch(self) -> list[str]:
        # Visit only catalogue branches, never individual group timetables.
        pending, visited, groups = {PORTAL_URL}, set(), set()
        semaphore = asyncio.Semaphore(4)
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
            async def read(url: str):
                async with semaphore:
                    response = await client.get(url)
                    response.raise_for_status()
                    return directory_links(response.text, PORTAL_URL)
            while pending:
                visited.update(pending)
                if len(visited) > 128:
                    raise ValueError('Group catalogue exceeded page limit')
                pages = await asyncio.gather(*(read(url) for url in sorted(pending)), return_exceptions=True)
                pending = set()
                for result in pages:
                    if isinstance(result, BaseException):
                        raise result
                    found, branches = result
                    groups.update(found)
                    pending.update(branches - visited)
        if not groups:
            raise ValueError('Empty group directory')
        return sorted(groups)

    async def _restore(self):
        try:
            async with get_pool().acquire() as conn:
                row = await conn.fetchrow("SELECT groups, saved_at FROM group_catalogue WHERE id=1")
            if row and row['saved_at'] > datetime.now(timezone.utc) - timedelta(days=7):
                groups = json.loads(row['groups']) if isinstance(row['groups'], str) else row['groups']
                if isinstance(groups, list) and groups and all(isinstance(g, str) and 0 < len(g) <= 100 for g in groups):
                    self._groups, self._saved_at = groups, row['saved_at']
        except Exception:
            pass  # No durable snapshot available; never accept arbitrary groups.

    async def _persist(self):
        try:
            async with get_pool().acquire() as conn:
                await conn.execute("INSERT INTO group_catalogue(id,groups,saved_at) VALUES(1,$1::jsonb,$2) ON CONFLICT(id) DO UPDATE SET groups=excluded.groups,saved_at=excluded.saved_at",
                                   json.dumps(self._groups), self._saved_at)
        except Exception:
            pass

    def _usable(self):
        return self._groups and self._saved_at and self._saved_at > datetime.now(timezone.utc) - timedelta(days=7)

    async def _refresh_cached(self, loader):
        try:
            groups = await asyncio.wait_for(loader(), timeout=50)
            async with self._lock:
                self._groups = groups
                self._saved_at = datetime.now(timezone.utc)
                self._expires = time.monotonic() + self.ttl
                self.stale = False
                await self._persist()
        except (httpx.HTTPError, ValueError, TimeoutError):
            self._retry_after = time.monotonic() + 60

    async def close(self):
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

    async def get(self) -> list[str]:
        async with self._lock:
            if self._groups is None:
                await self._restore()
            now = time.monotonic()
            if self._groups is not None and now < self._expires:
                return self._groups
            if self._usable():
                self.stale = True
                if now >= self._retry_after and (self._refresh_task is None or self._refresh_task.done()):
                    # Serve the verified snapshot immediately. Refresh cannot block login.
                    self._refresh_task = asyncio.create_task(self._refresh_cached(self._fetch))
                return self._groups
            if now < self._retry_after:
                raise HTTPException(503, detail={'code': 'group_directory_unavailable'})
            try:
                groups = await asyncio.wait_for(self._fetch(), timeout=50)
            except (httpx.HTTPError, ValueError, TimeoutError):
                self._retry_after = time.monotonic() + 60
                if self._usable():
                    self.stale = True
                    return self._groups
                raise HTTPException(503, detail={'code': 'group_directory_unavailable'}) from None
            self.stale = False
            self._saved_at = datetime.now(timezone.utc)
            self._groups = groups
            self._expires = time.monotonic() + self.ttl
            await self._persist()
            return groups

    async def validate(self, value: str) -> str:
        matches = [group for group in await self.get() if group_key(group) == group_key(value)]
        if len(matches) != 1:
            if self.stale:
                raise HTTPException(503, detail={'code': 'group_directory_unavailable'})
            raise HTTPException(422, detail={'code': 'unknown_group'})
        return matches[0]


directory = GroupDirectory()
router = APIRouter(prefix='/api/v1/groups', tags=['groups'])


@router.get('')
async def list_groups():
    return {'groups': await directory.get(), 'stale': directory.stale}
