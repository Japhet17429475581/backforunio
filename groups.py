"""Public group catalogue; membership is independent of timetable contents."""
from __future__ import annotations

import asyncio
import time
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

    async def get(self) -> list[str]:
        async with self._lock:
            now = time.monotonic()
            if self._groups is not None and now < self._expires:
                return self._groups
            if now < self._retry_after:
                raise HTTPException(503, detail={'code': 'group_directory_unavailable'})
            try:
                groups = await asyncio.wait_for(self._fetch(), timeout=50)
            except (httpx.HTTPError, ValueError, TimeoutError):
                self._retry_after = time.monotonic() + 60
                raise HTTPException(503, detail={'code': 'group_directory_unavailable'}) from None
            self._groups = groups
            self._expires = time.monotonic() + self.ttl
            return groups

    async def validate(self, value: str) -> str:
        matches = [group for group in await self.get() if group_key(group) == group_key(value)]
        if len(matches) != 1:
            raise HTTPException(422, detail={'code': 'unknown_group'})
        return matches[0]


directory = GroupDirectory()
router = APIRouter(prefix='/api/v1/groups', tags=['groups'])


@router.get('')
async def list_groups():
    return {'groups': await directory.get()}
