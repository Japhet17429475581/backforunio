"""Opt-in Web Push for installed PWA. Payloads omit personal document contents."""
import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone, date
from urllib.parse import urlsplit
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from pywebpush import webpush, WebPushException

from auth_api.auth import _user_from_token, _token_hash
from database.connection import get_pool
from groups import directory
from server_logging import log_event
from web_portal import check_origin, cipher, fail, client, upstream, restore_cookies, allowed_path

router = APIRouter(prefix='/api/v1/web-push', tags=['web-push'])
ZONE = timezone(timedelta(hours=3))


def configured():
    return bool(os.getenv('WEB_PUSH_PUBLIC_KEY') and os.getenv('WEB_PUSH_PRIVATE_KEY') and os.getenv('WEB_PORTAL_KEYS'))


def endpoint_hash(value):
    uri = urlsplit(value)
    host = uri.hostname or ''
    allowed = host in ('fcm.googleapis.com', 'updates.push.services.mozilla.com', 'web.push.apple.com') or host.endswith('.notify.windows.com')
    if uri.scheme != 'https' or not allowed or uri.port not in (443, None) or uri.username or uri.password:
        fail(422, 'push_endpoint_denied')
    return hashlib.sha256(value.encode()).hexdigest()


class Subscription(BaseModel):
    model_config = ConfigDict(extra='forbid')
    endpoint: str = Field(max_length=2048)
    p256dh: str = Field(min_length=20, max_length=160, pattern=r'^[A-Za-z0-9_\-]+=*$')
    auth: str = Field(min_length=10, max_length=64, pattern=r'^[A-Za-z0-9_\-]+=*$')
    kind: str = Field(pattern=r'^(group|teacher)$')
    owner: str = Field(min_length=1, max_length=255)


@router.get('/config')
async def config(request: Request):
    if not configured():
        return {'enabled': False}
    check_origin(request)
    return {'enabled': True, 'public_key': os.environ['WEB_PUSH_PUBLIC_KEY']}


@router.post('/subscription')
async def subscribe(data: Subscription, request: Request):
    check_origin(request)
    if not configured():
        fail(503, 'push_not_configured')
    user = await _user_from_token(request.headers.get('x-unio-token'))
    owner = await directory.validate(data.owner) if data.kind == 'group' else data.owner.strip()
    digest = endpoint_hash(data.endpoint)
    subscription = {'endpoint': data.endpoint, 'keys': {'p256dh': data.p256dh, 'auth': data.auth}}
    encrypted = cipher().encrypt(json.dumps(subscription).encode())
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            active = await conn.fetchval('SELECT id FROM users WHERE id=$1 AND account_token_hash=$2 AND token_revoked_at IS NULL AND token_expires_at>now() FOR UPDATE', user['id'], _token_hash(request.headers.get('x-unio-token')))
            if active is None:
                fail(401, 'web_auth_required')
            previous = await conn.fetchrow('SELECT id,user_id FROM web_push_subscriptions WHERE endpoint_hash=$1', digest)
            if previous and previous['user_id'] == user['id']:
                await conn.execute('UPDATE web_push_subscriptions SET subscription_cipher=$2,kind=$3,owner=$4,updated_at=now() WHERE id=$1', previous['id'], encrypted, data.kind, owner)
            else:
                await conn.execute('DELETE FROM web_push_subscriptions WHERE endpoint_hash=$1', digest)
                await conn.execute('INSERT INTO web_push_subscriptions(user_id,endpoint_hash,subscription_cipher,kind,owner) VALUES($1,$2,$3,$4,$5)', user['id'], digest, encrypted, data.kind, owner)
    return {'success': True}


@router.delete('/subscription')
async def unsubscribe(request: Request):
    check_origin(request)
    user = await _user_from_token(request.headers.get('x-unio-token'))
    async with get_pool().acquire() as conn:
        await conn.execute('DELETE FROM web_push_subscriptions WHERE user_id=$1', user['id'])
    return {'success': True}


async def deliver(conn, row, event_key, screen):
    inserted = await conn.fetchval('INSERT INTO web_push_deliveries(subscription_id,event_key) VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING event_key', row['id'], event_key)
    if inserted is None:
        return
    try:
        subscription = json.loads(cipher().decrypt(bytes(row['subscription_cipher'])))
        body = 'Скоро начнётся пара. Открой расписание.' if screen == 'schedule' else 'Обновился статус заявления. Проверь его в UNIO.'
        await asyncio.to_thread(webpush, subscription_info=subscription,
            data=json.dumps({'title': 'UNIO', 'body': body, 'screen': screen}, ensure_ascii=False),
            vapid_private_key=os.environ['WEB_PUSH_PRIVATE_KEY'],
            vapid_claims={'sub': os.getenv('WEB_PUSH_SUBJECT', 'mailto:unio.support@gmail.com')},
            ttl=180, timeout=15)
    except WebPushException as error:
        if error.response is not None and error.response.status_code in (404, 410):
            await conn.execute('DELETE FROM web_push_subscriptions WHERE id=$1', row['id'])
        else:
            await conn.execute('DELETE FROM web_push_deliveries WHERE subscription_id=$1 AND event_key=$2', row['id'], event_key)
    except Exception:
        await conn.execute('DELETE FROM web_push_deliveries WHERE subscription_id=$1 AND event_key=$2', row['id'], event_key)


def due_events(lessons, current):
    today = sorted((item for item in lessons if item.date == current.date()), key=lambda item: item.start.zfill(5))
    for index, item in enumerate(today):
        try:
            hour, minute = map(int, item.start.split(':'))
            starts = datetime.combine(item.date, datetime.min.time(), ZONE).replace(hour=hour, minute=minute)
        except (ValueError, TypeError):
            continue
        due = starts - timedelta(minutes=60 if index == 0 else 10)
        if timedelta(0) <= current - due <= timedelta(seconds=120):
            yield f'{item.date}:{item.start}'


async def poll_statuses(conn, row):
    if row['last_status_at'] and datetime.now(timezone.utc) - row['last_status_at'] < timedelta(minutes=30):
        return
    session_row = await conn.fetchrow('SELECT * FROM web_portal_sessions WHERE user_id=$1 AND expires_at>now() ORDER BY created_at DESC LIMIT 1', row['user_id'])
    if session_row is None:
        return
    await conn.execute('UPDATE web_push_subscriptions SET last_status_at=now() WHERE id=$1', row['id'])
    try:
        # Share the same row lock as foreground requests so cookie rotations agree.
        async with conn.transaction():
            session_row = await conn.fetchrow('SELECT * FROM web_portal_sessions WHERE id=$1 FOR UPDATE', session_row['id'])
            if session_row is None:
                return
            async with client() as portal:
                restore_cookies(portal, session_row['cookie_cipher'])
                current, seen, address = {}, set(), '/request/index'
                for _ in range(20):
                    address = allowed_path(address, 'GET')
                    if address in seen:
                        break
                    seen.add(address)
                    page = await upstream(portal, address)
                    document = BeautifulSoup(page.text, 'html.parser')
                    if document.select_one('#login-form') or not document.select_one('#category-selector,.requestItem,.list-view'):
                        return
                    current.update({item.get('data-key'): ' '.join(item.select_one('.Ach-Status').stripped_strings)
                                    for item in document.select('.requestItem[data-key]') if item.select_one('.Ach-Status')})
                    next_page = document.select_one('.pagination .next:not(.disabled) a[href],a[rel=next][href]')
                    if next_page is None:
                        break
                    address = urljoin(str(page.url), next_page['href'])
                old = json.loads(cipher().decrypt(bytes(row['status_cipher']))) if row['status_cipher'] else None
                from web_portal import pack_cookies
                await conn.execute('UPDATE web_portal_sessions SET cookie_cipher=$2 WHERE id=$1', session_row['id'], pack_cookies(portal))
                await conn.execute('UPDATE web_push_subscriptions SET status_cipher=$2,last_status_at=now() WHERE id=$1', row['id'], cipher().encrypt(json.dumps(current).encode()))
        if old is not None:
            for identifier, status in current.items():
                if identifier in old and status != old[identifier]:
                    digest = hashlib.sha256(f'{identifier}:{status}'.encode()).hexdigest()
                    await deliver(conn, row, 'request:' + digest, 'requests')
    except Exception:
        # No account identifiers, portal bodies or exception strings are logged.
        await conn.execute('UPDATE web_push_subscriptions SET last_status_at=now() WHERE id=$1', row['id'])


async def tick(app):
    current = datetime.now(ZONE)
    async with get_pool().acquire() as conn:
        locked = await conn.fetchval('SELECT pg_try_advisory_lock(742903)')
        if not locked:
            return
        try:
            rows = await conn.fetch('SELECT p.* FROM web_push_subscriptions p JOIN users u ON p.user_id=u.id WHERE u.token_revoked_at IS NULL AND u.token_expires_at>now() ORDER BY p.id LIMIT 500')
            schedules = {}
            for row in rows:
                key = (row['kind'], row['owner'])
                try:
                    if key not in schedules:
                        schedules[key], _, _ = await app.state.get_lessons(kind=row['kind'], value=row['owner'], target=current.date(), force_refresh=False)
                    for event in due_events(schedules[key], current):
                        owner_key = hashlib.sha256(f"{row['kind']}:{row['owner']}".encode()).hexdigest()[:16]
                        await deliver(conn, row, owner_key + ':' + event, 'schedule')
                    await poll_statuses(conn, row)
                except Exception as error:
                    log_event('web_push_subscription_failed', level=30, error=error)
                    continue
            await conn.execute("DELETE FROM web_push_deliveries WHERE created_at<now()-interval '10 days'")
            await conn.execute("DELETE FROM web_push_subscriptions WHERE updated_at<now()-interval '30 days'")
        finally:
            await conn.execute('SELECT pg_advisory_unlock(742903)')


async def loop(app):
    while True:
        if configured():
            try:
                await tick(app)
            except Exception as error:
                log_event('web_push_iteration_failed', level=30, error=error)
        await asyncio.sleep(60)
