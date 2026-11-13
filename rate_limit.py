from __future__ import annotations

import time
import asyncio
from fastapi import HTTPException
from redis_db import get_redis
from server_logging import log_event

_local = {}
_SCRIPT = """local n=redis.call('INCR',KEYS[1]);
if n==1 or redis.call('TTL',KEYS[1])<0 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end;
return {n,redis.call('TTL',KEYS[1])}"""

async def enforce_rate_limit(*, key: str, limit: int, window_seconds: int) -> None:
    try:
        async with asyncio.timeout(0.35):
            count, ttl = await get_redis().eval(_SCRIPT, 1, f'rate:{key}', window_seconds)
    except Exception as error:
        log_event('rate_limit_failed', level=30, error=error)
        now = time.monotonic()
        for expired in [k for k, (_, until) in _local.items() if until <= now]:
            _local.pop(expired, None)
        if key not in _local and len(_local) >= 10000:
            raise HTTPException(503, detail={'code': 'limiter_unavailable'})
        previous, until = _local.get(key, (0, now + window_seconds))
        count, ttl = previous + 1, max(1, int(until - now))
        _local[key] = (count, until)
    if count > limit:
        raise HTTPException(429, detail={'code': 'rate_limited'},
                            headers={'Retry-After': str(max(1, ttl))})
