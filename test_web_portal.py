import json
import os
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4
from urllib.parse import parse_qs

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from web_portal import (router, body_guard, cipher, checked_url, allowed_path,
    pack_cookies, restore_cookies, issue_token, ACCESS_COOKIE, REFRESH_COOKIE)
from privacy import DOCUMENTS
from web_push import endpoint_hash, due_events


class Context:
    def __init__(self, value): self.value = value
    async def __aenter__(self): return self.value
    async def __aexit__(self, *args): return False


def database(row=None):
    conn = Mock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=7)
    conn.fetchrow = AsyncMock(return_value=row)
    conn.transaction.return_value = Context(conn)
    pool = Mock()
    pool.acquire.return_value = Context(conn)
    return pool, conn


class WebPortalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {'WEB_PORTAL_KEYS': Fernet.generate_key().decode(),
            'WEB_PORTAL_ORIGINS': 'https://unio.test'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.middleware('http')(body_guard)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
            base_url='https://unio.test', headers={'Origin': 'https://unio.test', 'X-Unio-Web': '1'})
        self.addAsyncCleanup(self.http.aclose)

    def receipt(self, document):
        return {'event_id': str(uuid4()), 'document': document, **DOCUMENTS[document],
            'accepted': True, 'occurred_at': datetime.now(timezone.utc).isoformat(), 'origin': 'user'}

    def test_exact_origin_paths_and_encrypted_cookie_roundtrip(self):
        self.assertEqual(checked_url('http://portal.vspu.ru/request/index'), 'https://portal.vspu.ru/request/index')
        for value in ('https://evil.test/', 'https://portal.vspu.ru.evil.test/', 'https://user@portal.vspu.ru/', 'https://portal.vspu.ru:444/'):
            with self.assertRaises(HTTPException): checked_url(value)
        with self.assertRaises(HTTPException): allowed_path('/request/create', 'GET')
        with self.assertRaises(HTTPException): allowed_path('/admin', 'POST')
        self.assertTrue(allowed_path('/progress/index', 'GET').endswith('/progress/index'))
        source = httpx.AsyncClient()
        source.cookies.set('session', 'university-test-secret', domain='portal.vspu.ru', path='/')
        encrypted = pack_cookies(source)
        self.assertNotIn(b'university-test-secret', encrypted)
        target = httpx.AsyncClient()
        restore_cookies(target, encrypted)
        self.assertEqual(target.cookies.get('session'), 'university-test-secret')

    def test_login_remains_rate_limited_without_redis(self):
        from starlette.requests import Request
        from web_portal import limit_login_locally, _login_windows
        request = Request({'type': 'http', 'client': ('synthetic-test-client', 1), 'headers': []})
        with patch.dict(_login_windows, {}, clear=True):
            for _ in range(8):
                limit_login_locally(request)
            with self.assertRaises(HTTPException) as caught:
                limit_login_locally(request)
            self.assertEqual(caught.exception.status_code, 429)

    async def test_origin_and_size_rejected_before_database_or_portal(self):
        response = await self.http.post('/api/v1/web-portal/refresh', headers={'Origin': 'https://evil.test'})
        self.assertEqual(response.status_code, 403)
        self.assertIn('no-store', response.headers['cache-control'])
        response = await self.http.post('/api/v1/web-portal/login', content=b'x' * 65537)
        self.assertEqual(response.status_code, 413)

    async def test_login_only_persists_cipher_and_hashes(self):
        def university(request):
            if request.method == 'GET':
                return httpx.Response(200, text='<form id="login-form" action="/login"><input type="hidden" name="_csrf" value="csrf-test"></form>', headers={'content-type': 'text/html'})
            data = parse_qs(request.content.decode())
            self.assertEqual(data['LoginForm[password]'], ['test-password'])
            self.assertEqual(data['RememberMe'], ['1'])
            return httpx.Response(200, text='<form action="/auth/logout"></form>', headers={'set-cookie': 'session=university-test-secret; Path=/; Secure', 'content-type': 'text/html'})
        @asynccontextmanager
        async def portal():
            async with httpx.AsyncClient(transport=httpx.MockTransport(university)) as session: yield session
        pool, conn = database()
        with patch('web_portal.client', portal), patch('web_portal.get_pool', return_value=pool), patch('web_portal.directory.validate', AsyncMock(return_value='TEST')):
            response = await self.http.post('/api/v1/web-portal/login', json={'login': 'test-student', 'password': 'test-password', 'group': 'TEST', 'consents': [self.receipt('terms'), self.receipt('personal_data')]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('account_token', response.json())
        headers = '\n'.join(response.headers.get_list('set-cookie')).lower()
        self.assertIn('httponly', headers)
        self.assertIn('secure', headers)
        self.assertIn('samesite=strict', headers)
        persisted = repr(conn.execute.call_args_list) + repr(conn.fetchval.call_args_list)
        self.assertNotIn('test-password', persisted)
        self.assertNotIn('university-test-secret', persisted)
        self.assertNotIn('test-student', persisted)

    async def test_relay_restores_cookie_and_forwards_upload_without_persistence(self):
        identifier = uuid4()
        cookie, digest = issue_token(identifier)
        async with httpx.AsyncClient() as source:
            source.cookies.set('session', 'test-cookie', domain='portal.vspu.ru', path='/')
            encrypted = pack_cookies(source)
        row = {'id': identifier, 'user_id': 7, 'access_hash': digest,
               'expires_at': datetime.now(timezone.utc) + timedelta(days=29),
               'access_expires_at': datetime.now(timezone.utc) + timedelta(minutes=10),
               'cookie_cipher': encrypted}
        calls = []
        def university(request):
            calls.append(request)
            self.assertIn('session=test-cookie', request.headers.get('cookie', ''))
            if request.method == 'POST':
                self.assertIn(b'test-owned-file-bytes', request.content)
                self.assertEqual(request.headers['X-CSRF-Token'], 'csrf-test')
                return httpx.Response(200, json={'ok': True})
            return httpx.Response(200, text='<div class="list-view">test-owned-html</div>', headers={'content-type': 'text/html'})
        @asynccontextmanager
        async def portal():
            async with httpx.AsyncClient(transport=httpx.MockTransport(university)) as session:
                yield session
        pool, conn = database(row)
        self.http.cookies.set(ACCESS_COOKIE, cookie)
        with patch('web_portal.get_pool', return_value=pool), patch('web_portal.client', portal):
            response = await self.http.post('/api/v1/web-portal/request', json={'path': '/request/index'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('test-owned-html', response.json()['data'])
            response = await self.http.post('/api/v1/web-portal/request',
                data={'payload': json.dumps({'path': '/request/101/upload', 'ajax': True, 'csrf': 'csrf-test'})},
                files={'attachment': ('test.pdf', b'test-owned-file-bytes', 'application/pdf')})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(calls), 2)
        persisted = repr(conn.execute.call_args_list)
        self.assertNotIn('test-owned-file-bytes', persisted)
        self.assertNotIn('test-owned-html', persisted)
        self.assertIn('no-store', response.headers['cache-control'])

    async def test_external_redirect_does_not_forward_login_password(self):
        calls = []
        def university(request):
            calls.append(request)
            return httpx.Response(307, headers={'location': 'https://evil.test/'})
        from web_portal import upstream
        async with httpx.AsyncClient(transport=httpx.MockTransport(university)) as session:
            with self.assertRaises(HTTPException):
                await upstream(session, '/login', fields={'password': 'synthetic-private'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].url.host, 'portal.vspu.ru')

    async def test_refresh_rotates_and_replay_commits_revocation(self):
        identifier = uuid4()
        cookie, digest = issue_token(identifier)
        row = {'id': identifier, 'user_id': 7, 'refresh_hash': digest,
            'expires_at': datetime.now(timezone.utc) + timedelta(days=29)}
        pool, conn = database(row)
        self.http.cookies.set(REFRESH_COOKIE, cookie)
        with patch('web_portal.get_pool', return_value=pool):
            response = await self.http.post('/api/v1/web-portal/refresh')
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.cookies.get(REFRESH_COOKIE), cookie)
        self.assertTrue(any('web_portal_refresh_used' in call.args[0] for call in conn.execute.call_args_list))
        conn.execute.reset_mock()
        row['refresh_hash'] = 'a-different-current-hash'
        conn.fetchval.return_value = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.http.cookies.clear()
        self.http.cookies.set(REFRESH_COOKIE, cookie)
        with patch('web_portal.get_pool', return_value=pool):
            response = await self.http.post('/api/v1/web-portal/refresh')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['detail']['code'], 'refresh_reused')
        self.assertTrue(any('DELETE FROM web_portal_sessions' in call.args[0] for call in conn.execute.call_args_list))

    async def test_expired_access_does_not_execute_mutating_portal_action(self):
        identifier = uuid4()
        cookie, digest = issue_token(identifier)
        row = {'id': identifier, 'user_id': 7, 'access_hash': digest,
            'access_expires_at': datetime.now(timezone.utc) - timedelta(seconds=1),
            'expires_at': datetime.now(timezone.utc) + timedelta(days=1)}
        pool, _ = database(row)
        self.http.cookies.set(ACCESS_COOKIE, cookie)
        upstream = AsyncMock()
        with patch('web_portal.get_pool', return_value=pool), patch('web_portal.upstream', upstream):
            response = await self.http.post('/api/v1/web-portal/request', json={'path': '/request/create', 'fields': {'type': '1'}})
        self.assertEqual(response.status_code, 401)
        upstream.assert_not_called()

    def test_push_targets_cannot_be_used_as_an_open_proxy(self):
        for value in ('https://127.0.0.1/a', 'http://web.push.apple.com/a', 'https://fcm.googleapis.com.evil.test/a', 'https://user@fcm.googleapis.com/a'):
            with self.assertRaises(HTTPException): endpoint_hash(value)
        self.assertEqual(len(endpoint_hash('https://web.push.apple.com/test-owned-endpoint')), 64)

    def test_schedule_push_uses_university_timezone_and_existing_reminder_rules(self):
        from types import SimpleNamespace
        zone = timezone(timedelta(hours=3))
        current = datetime(2026, 10, 22, 8, 40, tzinfo=zone)
        lessons = [SimpleNamespace(date=current.date(), start='9:40'), SimpleNamespace(date=current.date(), start='11:30')]
        self.assertEqual(list(due_events(lessons, current)), ['2026-10-22:9:40'])
        self.assertEqual(list(due_events(lessons, current.replace(hour=11, minute=20))), ['2026-10-22:11:30'])


if __name__ == '__main__': unittest.main()
