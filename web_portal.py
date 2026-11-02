"""Web-only portal bridge. APK direct authentication stays independent.

Passwords are used only during login; only authenticated-encrypted cookies and
token hashes are durable. No university HTML or attachments are cached.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from http.cookiejar import Cookie
from urllib.parse import urljoin, urlsplit, urlencode
from uuid import UUID, uuid4

import httpx
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, MultiFernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile

from database.connection import get_pool
from auth_api.auth import _user_from_token, _new_token, _token_hash
from groups import directory
from privacy import ConsentRecord, validate_consents, save_consents
from rate_limit import enforce_rate_limit
from server_logging import log_event

PREFIX = '/api/v1/web-portal'
ORIGIN = 'https://portal.vspu.ru'
ACCESS_COOKIE = '__Secure-unio_portal_access'
REFRESH_COOKIE = '__Secure-unio_portal_refresh'
ACCESS_SECONDS = 15 * 60
REFRESH_DAYS = 30
MAX_BODY = 22 * 1024 * 1024
MAX_RESPONSE = 5 * 1024 * 1024
router = APIRouter(prefix=PREFIX, tags=['web-portal'])
_portal_slots = asyncio.Semaphore(3)
_login_windows = {}


def limit_login_locally(request):
    # Keep a small per-process safeguard even when the shared Redis limiter fails.
    current = time.monotonic()
    for key, value in list(_login_windows.items()):
        if current - value[0] >= 60:
            del _login_windows[key]
    host = request.client.host if request.client else 'unknown'
    if host not in _login_windows:
        if len(_login_windows) >= 10000:
            fail(429, 'web_login_rate_limited')
        _login_windows[host] = [current, 0]
    _login_windows[host][1] += 1
    if _login_windows[host][1] > 8:
        fail(429, 'web_login_rate_limited')


def now():
    return datetime.now(timezone.utc)


def fail(status, code):
    raise HTTPException(status, detail={'code': code})


def cipher():
    try:
        keys = [part.strip() for part in os.getenv('WEB_PORTAL_KEYS', '').split(',') if part.strip()]
        if not keys:
            fail(503, 'web_portal_not_configured')
        return MultiFernet([Fernet(key.encode()) for key in keys])
    except (ValueError, TypeError):
        fail(503, 'web_portal_not_configured')


def allowed_origins():
    return {item.strip().rstrip('/') for item in os.getenv('WEB_PORTAL_ORIGINS', '').split(',') if item.strip()}


def check_origin(request):
    allowed = allowed_origins()
    origin = request.headers.get('origin', '').rstrip('/')
    if not origin and request.headers.get('sec-fetch-site') == 'same-origin':
        origin = f"{request.headers.get('x-forwarded-proto', request.url.scheme)}://{request.headers.get('x-forwarded-host', request.headers.get('host', ''))}"
    if not allowed:
        fail(503, 'web_portal_not_configured')
    if origin not in allowed or request.headers.get('x-unio-web') != '1':
        fail(403, 'web_origin_denied')
    cipher()


async def body_guard(request, call_next):
    if not request.url.path.startswith((PREFIX, '/api/v1/web-push', '/api/v1/diagnostics', '/api/v1/assistant')):
        return await call_next(request)
    from starlette.responses import JSONResponse
    limit = MAX_BODY if request.url.path.endswith('/request') else 65536
    try:
        size = int(request.headers.get('content-length', '0'))
        if size > limit:
            return JSONResponse({'detail': {'code': 'payload_too_large'}}, status_code=413,
                                headers={'Cache-Control': 'no-store'})
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > limit:
                return JSONResponse({'detail': {'code': 'payload_too_large'}}, status_code=413,
                                    headers={'Cache-Control': 'no-store'})
        request._body = bytes(chunks)
        response = await call_next(request)
    except (ValueError, httpx.HTTPError):
        response = JSONResponse({'detail': {'code': 'web_request_failed'}}, status_code=502)
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


def checked_url(value):
    absolute = urljoin(ORIGIN + '/', value)
    uri = urlsplit(absolute)
    if uri.hostname == 'portal.vspu.ru' and uri.scheme == 'http' and uri.port in (None, 80):
        absolute = absolute.replace('http://', 'https://', 1)
        uri = urlsplit(absolute)
    if uri.scheme != 'https' or uri.hostname != 'portal.vspu.ru' or uri.port not in (None, 443) or uri.username or uri.password:
        fail(502, 'portal_redirect_denied')
    return absolute


def has_portal_session(document):
    for element in document.select('form[action], a[href]'):
        try:
            if urlsplit(checked_url(element.get('action') if element.name == 'form' else element.get('href'))).path == '/auth/logout':
                return True
        except HTTPException:
            continue
    return False


def allowed_path(value, method):
    absolute = checked_url(value)
    path = urlsplit(absolute).path
    read = path in ('/progress/index', '/request/index', '/request/get-types') or bool(re.fullmatch(r'/request/\d+/edit', path))
    write = path == '/request/create' or bool(re.fullmatch(r'/request/\d+/(edit|upload)', path))
    if not ((method == 'GET' and read) or (method == 'POST' and write)):
        fail(403, 'portal_path_denied')
    return absolute


@asynccontextmanager
async def client():
    async with _portal_slots:
        async with httpx.AsyncClient(timeout=httpx.Timeout(25, connect=10), follow_redirects=False,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36',
                     'Accept-Language': 'ru-RU,ru;q=0.9'}) as connection:
            yield connection


async def upstream(session, value, *, fields=None, files=None, ajax=False, referer=None, csrf=None, follow=True):
    uri = checked_url(value)
    method = 'POST' if fields is not None or files else 'GET'
    path = urlsplit(uri).path
    operation = ('login' if path in ('/login', '/auth/login') else
                 'requests_list' if path == '/request/index' else
                 'request_types' if path == '/request/get-types' else
                 'request_create' if path == '/request/create' else
                 'request_form' if re.fullmatch(r'/request/\d+/edit', path) else
                 'other')
    for _ in range(6):
        headers = {}
        if method == 'POST':
            headers['Origin'] = ORIGIN
        if referer:
            headers['Referer'] = checked_url(referer)
        if ajax:
            headers['X-Requested-With'] = 'XMLHttpRequest'
            if csrf:
                headers['X-CSRF-Token'] = csrf
        kwargs = {'headers': headers}
        if method == 'POST':
            if files:
                kwargs.update(data=fields or {}, files=files)
            else:
                kwargs['content'] = urlencode(fields or {}, doseq=True).encode()
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
        try:
            async with session.stream(method, uri, **kwargs) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE:
                        fail(502, 'portal_response_too_large')
                result = httpx.Response(response.status_code, headers=response.headers,
                    content=bytes(content), request=response.request)
        except httpx.HTTPError as error:
            log_event('portal_upstream_failed', level=30, operation=operation, error=error)
            fail(502, 'portal_unavailable')
        if result.status_code not in (301, 302, 303, 307, 308):
            log_event('portal_upstream_response', operation=operation, status=result.status_code,
                      level=30 if result.status_code >= 400 else 20)
            if result.status_code >= 400:
                fail(502, 'portal_http_error')
            return result
        location = result.headers.get('location')
        if not location:
            fail(502, 'portal_invalid_redirect')
        uri = checked_url(urljoin(uri, location))
        if not follow:
            return result
        if files:
            fail(502, 'portal_upload_redirect')
        if result.status_code in (301, 302, 303):
            method, fields = 'GET', None
    fail(502, 'portal_redirect_loop')


def pack_cookies(session):
    items = [dict(name=c.name, value=c.value, domain=c.domain, path=c.path,
                  secure=c.secure, expires=c.expires) for c in session.cookies.jar]
    return cipher().encrypt(json.dumps(items).encode())


def restore_cookies(session, encrypted):
    try:
        items = json.loads(cipher().decrypt(bytes(encrypted)))
        for item in items:
            session.cookies.jar.set_cookie(Cookie(0, item['name'], item['value'], None, False,
                item['domain'], True, item['domain'].startswith('.'), item['path'], True,
                item['secure'], item['expires'], item['expires'] is None, None, None, {}))
    except (InvalidToken, ValueError, TypeError, KeyError):
        fail(401, 'portal_reauthentication_required')


def issue_token(session_id):
    raw = secrets.token_urlsafe(32)
    return f'{session_id}.{raw}', hashlib.sha256(raw.encode()).hexdigest()


def split_token(value):
    try:
        if not value or len(value) > 160:
            fail(401, 'web_auth_required')
        identifier, raw = value.split('.', 1)
        return UUID(identifier), hashlib.sha256(raw.encode()).hexdigest()
    except (ValueError, AttributeError):
        fail(401, 'web_auth_required')


def set_cookies(response, access, refresh, expires):
    remaining = max(0, int((expires - now()).total_seconds()))
    options = dict(secure=True, httponly=True, samesite='strict', path=PREFIX)
    response.set_cookie(ACCESS_COOKIE, access, max_age=min(ACCESS_SECONDS, remaining), **options)
    response.set_cookie(REFRESH_COOKIE, refresh, max_age=remaining, **options)


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    login: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=4096, repr=False)
    group: str | None = Field(default=None, max_length=100)
    teacher_fio: str | None = Field(default=None, max_length=255)
    account_token: str | None = Field(default=None, max_length=256, repr=False)
    consents: list[ConsentRecord] = Field(max_length=100)


@router.post('/login')
async def login(data: Login, request: Request, response: Response):
    check_origin(request)
    limit_login_locally(request)
    await enforce_rate_limit(key=f'web-login:{request.client.host if request.client else "unknown"}', limit=8, window_seconds=60)
    validate_consents(data.consents, required=True)
    if bool(data.group) == bool(data.teacher_fio):
        fail(422, 'web_role_required')
    group = await directory.validate(data.group) if data.group else None
    async with client() as session:
        page = await upstream(session, '/login')
        document = BeautifulSoup(page.text, 'html.parser')
        form = document.select_one('#login-form')
        if form is None:
            fail(502, 'portal_login_unrecognized')
        fields = {item.get('name'): item.get('value', '') for item in form.select('input[type=hidden][name]')}
        param, token = document.select_one('meta[name=csrf-param]'), document.select_one('meta[name=csrf-token]')
        if param and token:
            fields[param.get('content')] = token.get('content')
        fields.update({'LoginForm[username]': data.login, 'LoginForm[password]': data.password, 'RememberMe': '1'})
        result = await upstream(session, form.get('action') or '/login', fields=fields, referer=str(page.url))
        fields.clear()
        logged_in = has_portal_session(BeautifulSoup(result.text, 'html.parser'))
        if not logged_in:
            fail(401, 'portal_login_rejected')
        encrypted = pack_cookies(session)
    existing = None
    if data.account_token:
        try:
            existing = await _user_from_token(data.account_token)
        except HTTPException as error:
            # An expired UNIO token must not prevent a fresh university login.
            if error.status_code != 401:
                raise
    own_token, own_hash = (data.account_token, _token_hash(data.account_token)) if existing else _new_token()
    identifier = uuid4()
    access, access_hash = issue_token(identifier)
    refresh, refresh_hash = issue_token(identifier)
    expires = now() + timedelta(days=REFRESH_DAYS)
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            if existing:
                user_id = await conn.fetchval('SELECT id FROM users WHERE account_token_hash=$1 AND token_revoked_at IS NULL AND token_expires_at>now() FOR UPDATE', own_hash)
                if user_id is None:
                    fail(401, 'web_auth_required')
                await conn.execute('UPDATE users SET token_expires_at=$2,group_code=$3 WHERE id=$1', user_id, expires, group)
            else:
                user_id = await conn.fetchval("INSERT INTO users(auth_mode,group_code,account_token_hash,token_created_at,token_expires_at) VALUES('guest',$1,$2,now(),$3) RETURNING id", group, own_hash, expires)
            await save_consents(conn, user_id, data.consents)
            await conn.execute('DELETE FROM web_portal_sessions WHERE user_id=$1', user_id)
            await conn.execute('INSERT INTO web_portal_sessions(id,user_id,cookie_cipher,access_hash,access_expires_at,refresh_hash,expires_at) VALUES($1,$2,$3,$4,$5,$6,$7)',
                identifier, user_id, encrypted, access_hash, now() + timedelta(seconds=ACCESS_SECONDS), refresh_hash, expires)
    set_cookies(response, access, refresh, expires)
    return {'account_token': own_token, 'group': group, 'expires_at': expires.isoformat()}


@router.post('/refresh')
async def refresh(request: Request, response: Response):
    check_origin(request)
    identifier, digest = split_token(request.cookies.get(REFRESH_COOKIE))
    outcome = None
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow('SELECT s.* FROM web_portal_sessions s JOIN users u ON u.id=s.user_id WHERE s.id=$1 AND u.token_revoked_at IS NULL AND u.token_expires_at>now() FOR UPDATE OF s', identifier)
            if row is None or row['expires_at'] <= now():
                fail(401, 'web_auth_required')
            if not hmac.compare_digest(row['refresh_hash'], digest):
                used = await conn.fetchval('SELECT used_at FROM web_portal_refresh_used WHERE session_id=$1 AND token_hash=$2', identifier, digest)
                if used and now() - used <= timedelta(seconds=10):
                    outcome = 'refresh_race'
                elif used:
                    await conn.execute('DELETE FROM web_portal_sessions WHERE id=$1', identifier)
                    outcome = 'refresh_reused'
                else:
                    outcome = 'web_auth_required'
            else:
                access, access_hash = issue_token(identifier)
                renewal, refresh_hash = issue_token(identifier)
                await conn.execute('INSERT INTO web_portal_refresh_used(session_id,token_hash) VALUES($1,$2)', identifier, digest)
                await conn.execute('UPDATE web_portal_sessions SET access_hash=$2,access_expires_at=$3,refresh_hash=$4 WHERE id=$1', identifier, access_hash, now() + timedelta(seconds=ACCESS_SECONDS), refresh_hash)
                await conn.execute('UPDATE users SET token_last_used_at=now() WHERE id=$1', row['user_id'])
    if outcome:
        fail(409 if outcome == 'refresh_race' else 401, outcome)
    set_cookies(response, access, renewal, row['expires_at'])
    return {'success': True}


async def access_row(conn, request):
    identifier, digest = split_token(request.cookies.get(ACCESS_COOKIE))
    row = await conn.fetchrow('SELECT s.* FROM web_portal_sessions s JOIN users u ON u.id=s.user_id WHERE s.id=$1 AND u.token_revoked_at IS NULL AND u.token_expires_at>now() FOR UPDATE OF s', identifier)
    if row is None or row['expires_at'] <= now() or row['access_expires_at'] <= now() or not hmac.compare_digest(row['access_hash'], digest):
        fail(401, 'web_access_expired')
    return row


class Relay(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(max_length=2048)
    fields: dict[str, str] | None = None
    ajax: bool = False
    follow: bool = True
    referer: str | None = Field(default=None, max_length=2048)
    csrf: str | None = Field(default=None, max_length=4096)


@router.post('/request')
async def relay(request: Request):
    check_origin(request)
    await enforce_rate_limit(key=f'web-request:{request.client.host if request.client else "unknown"}', limit=120, window_seconds=60)
    files = []
    if request.headers.get('content-type', '').startswith('multipart/form-data'):
        async with request.form(max_files=4, max_fields=8, max_part_size=21 * 1024 * 1024) as form:
            try:
                data = Relay.model_validate_json(form.get('payload', ''))
            except (ValidationError, TypeError):
                fail(422, 'invalid_request')
            for name, item in form.multi_items():
                if isinstance(item, UploadFile):
                    content = await item.read(20 * 1024 * 1024 + 1)
                    if not content or len(content) > 20 * 1024 * 1024:
                        fail(413, 'attachment_too_large')
                    filename = (item.filename or 'document').replace('\\', '/').split('/')[-1][:255]
                    files.append((name, (filename, content, item.content_type or 'application/octet-stream')))
    else:
        try:
            data = Relay.model_validate(await request.json())
        except (ValidationError, ValueError):
            fail(422, 'invalid_request')
    if data.fields is not None and (len(data.fields) > 150 or sum(len(k) + len(v) for k,v in data.fields.items()) > 65536):
        fail(413, 'payload_too_large')
    uri = allowed_path(data.path, 'POST' if data.fields is not None or files else 'GET')
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            row = await access_row(conn, request)
            async with client() as session:
                restore_cookies(session, row['cookie_cipher'])
                result = await upstream(session, uri, fields=data.fields, files=files or None,
                    ajax=data.ajax, referer=data.referer, csrf=data.csrf, follow=data.follow)
                document = BeautifulSoup(result.text, 'html.parser') if 'text/html' in result.headers.get('content-type', '') else None
                if document is not None and not has_portal_session(document) and document.select_one('#login-form, input[type=password]'):
                    fail(401, 'portal_reauthentication_required')
                await conn.execute('UPDATE web_portal_sessions SET cookie_cipher=$2 WHERE id=$1', row['id'], pack_cookies(session))
                await conn.execute('UPDATE users SET token_last_used_at=now() WHERE id=$1', row['user_id'])
    return {'status': result.status_code, 'data': result.text, 'url': str(result.url),
            'content_type': result.headers.get('content-type', ''),
            'location': checked_url(urljoin(str(result.url), result.headers['location'])) if 'location' in result.headers else None}


@router.post('/logout')
async def logout(request: Request, response: Response):
    check_origin(request)
    value = request.cookies.get(REFRESH_COOKIE)
    if value:
        identifier, digest = split_token(value)
        async with get_pool().acquire() as conn:
            await conn.execute('DELETE FROM web_portal_sessions WHERE id=$1 AND refresh_hash=$2', identifier, digest)
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(name, path=PREFIX, secure=True, httponly=True, samesite='strict')
    return {'success': True}
