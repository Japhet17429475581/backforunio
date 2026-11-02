import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException
from starlette.requests import Request
from groups import GroupDirectory, directory_links, PORTAL_URL


def page(query, label='Group', row=False):
    kind = 'row' if row else 'list'
    return (f'<div class="schedule-filter-{kind}"><p class="schedule-filter-{kind}__item">'
            f'<a href="{PORTAL_URL}?{urlencode(query)}">{label}</a></p></div>')


class GroupTests(unittest.IsolatedAsyncioTestCase):
    def test_links_use_actual_group_key_and_ignore_external_origins(self):
        content = page({'group': 'ИЯ-АВБ-111'}, 'short', True)
        groups, branches = directory_links(content, PORTAL_URL)
        self.assertEqual(groups, {'ИЯ-АВБ-111'})
        self.assertFalse(branches)
        self.assertFalse(directory_links(content.replace(PORTAL_URL, 'https://other.example/schedule/students'), PORTAL_URL)[0])
        with self.assertRaises(ValueError):
            directory_links('<html>Maintenance</html>', PORTAL_URL)

    async def test_walks_only_catalogue_branches_and_reuses_single_fetch(self):
        calls = []
        def respond(request):
            calls.append(str(request.url))
            self.assertNotIn('group', request.url.params)
            return httpx.Response(200, text=page({'code': 'ИЯ'}) if not request.url.query else
                                  page({'group': 'ИЯ-АВБ-111'}, row=True))
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        directory = GroupDirectory()
        with patch('groups.httpx.AsyncClient', return_value=client):
            results = await asyncio.gather(*(directory.get() for _ in range(5)))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(result == ['ИЯ-АВБ-111'] for result in results))
        self.assertEqual(await directory.validate(' ия авб 111 '), 'ИЯ-АВБ-111')
        with self.assertRaises(HTTPException) as failure:
            await directory.validate('111')
        self.assertEqual(failure.exception.status_code, 422)

    async def test_failed_or_partial_catalogue_is_unavailable_not_invalid_group(self):
        directory = GroupDirectory()
        with patch.object(directory, '_fetch', AsyncMock(side_effect=httpx.ConnectError('offline'))) as fetch:
            for _ in range(2):
                with self.assertRaises(HTTPException) as failure:
                    await directory.validate('ИЯ-АВБ-111')
                self.assertEqual(failure.exception.status_code, 503)
            self.assertEqual(fetch.await_count, 1)

    async def test_expired_cache_cannot_make_a_false_invalid_group_decision(self):
        directory = GroupDirectory()
        with patch.object(directory, '_fetch', AsyncMock(return_value=['A-111'])):
            await directory.get()
        directory._expires = 0
        with patch.object(directory, '_fetch', AsyncMock(side_effect=ValueError('changed markup'))):
            with self.assertRaises(HTTPException) as failure:
                await directory.validate('NEW-111')
            self.assertEqual(failure.exception.status_code, 503)

    async def test_one_failed_branch_discards_partial_results(self):
        def respond(request):
            code = request.url.params.get('code')
            if code == 'broken':
                return httpx.Response(502)
            return httpx.Response(200, text=page({'code': 'ok'}) + page({'code': 'broken'})
                                  if code is None else page({'group': 'VALID-111'}, row=True))
        directory = GroupDirectory()
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch('groups.httpx.AsyncClient', return_value=client):
            with self.assertRaises(HTTPException) as failure:
                await directory.get()
        self.assertEqual(failure.exception.status_code, 503)
        self.assertIsNone(directory._groups)

    async def test_auth_rejects_invalid_group_without_database_writes(self):
        from auth_api import auth
        request = Request({'type': 'http', 'client': ('127.0.0.1', 1234), 'headers': []})
        for status in [422, 503]:
            with patch.object(auth.group_directory, 'validate', AsyncMock(side_effect=HTTPException(status))), \
                 patch.object(auth, 'enforce_rate_limit', AsyncMock()), \
                 patch.object(auth, 'get_pool') as pool:
                with self.assertRaises(HTTPException):
                    await auth.guest_login(auth.GuestAuthRequest(group_code='WRONG'), request)
                pool.assert_not_called()
        self.assertFalse(hasattr(auth, 'portal_login'))


if __name__ == '__main__':
    unittest.main()
