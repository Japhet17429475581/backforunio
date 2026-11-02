from __future__ import annotations

import json
import secrets
from typing import Any

import redis.asyncio as redis

from config import REDIS_URL
from server_logging import log_event

_client: redis.Redis | None = None


async def init_redis() -> redis.Redis:
    global _client

    if _client is not None:
        return _client

    if not REDIS_URL:
        raise RuntimeError("REDIS_URL не задан в переменных окружения")

    _client = redis.from_url(
        REDIS_URL,
        decode_responses=True,
        health_check_interval=30,
        socket_connect_timeout=5,
        socket_timeout=5,
        retry_on_timeout=True,
    )
    try:
        await _client.ping()
    except BaseException:
        await close_redis()
        raise
    return _client


def get_redis() -> redis.Redis:
    if _client is None:
        raise RuntimeError("Redis ещё не подключён")
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        client = _client
        _client = None
        await client.aclose()


async def redis_get_json(key: str) -> Any | None:
    try:
        raw = await get_redis().get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        log_event('redis_operation_failed', level=30, error=exc)
        return None


async def redis_set_json(key: str, value: Any, ttl: int) -> bool:
    try:
        await get_redis().setex(
            key,
            ttl,
            json.dumps(value, ensure_ascii=False, default=str),
        )
        return True
    except Exception as exc:
        log_event('redis_operation_failed', level=30, error=exc)
        return False


async def redis_delete(*keys: str) -> None:
    if not keys:
        return
    try:
        await get_redis().delete(*keys)
    except Exception as exc:
        log_event('redis_operation_failed', level=30, error=exc)


async def redis_ping() -> bool:
    try:
        await get_redis().ping()
        return True
    except Exception:
        return False


async def redis_try_lock(key: str, ttl: int = 30) -> str | None:
    """Распределённый lock: только один worker обновляет ресурс."""
    token = secrets.token_urlsafe(16)
    try:
        acquired = await get_redis().set(key, token, ex=ttl, nx=True)
        return token if acquired else None
    except Exception as exc:
        log_event('redis_operation_failed', level=30, error=exc)
        return None


async def redis_unlock(key: str, token: str) -> None:
    # Удаляем только собственный lock, чтобы не снести lock другого worker.
    script = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('del', KEYS[1])
    end
    return 0
    """
    try:
        await get_redis().eval(script, 1, key, token)
    except Exception as exc:
        log_event('redis_operation_failed', level=30, error=exc)
