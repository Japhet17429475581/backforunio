from __future__ import annotations

from fastapi import HTTPException

from redis_db import get_redis
from server_logging import log_event


async def enforce_rate_limit(
    *,
    key: str,
    limit: int,
    window_seconds: int,
) -> None:
    """Простой Redis rate-limit по фиксированному окну."""
    try:
        redis = get_redis()
        redis_key = f"rate:{key}"
    except Exception as exc:
        log_event('rate_limit_failed', level=30, error=exc)
        return

    try:
        count = await redis.incr(redis_key)
        if count == 1:
            await redis.expire(redis_key, window_seconds)

        if count > limit:
            ttl = await redis.ttl(redis_key)
            raise HTTPException(
                status_code=429,
                detail=f"Слишком много запросов. Повторите через {max(ttl, 1)} сек.",
                headers={"Retry-After": str(max(ttl, 1))},
            )
    except HTTPException:
        raise
    except Exception as exc:
        # Redis rate-limit не должен ломать API, если Redis временно недоступен.
        log_event('rate_limit_failed', level=30, error=exc)
