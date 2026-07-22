"""Redis caching layer for frequently-read, infrequently-changed data.

Usage:
    from app.cache import redis_cache

    # Cache plan limits for 5 minutes
    limits = await redis_cache.get_or_set(
        "plan_limits",
        lambda: fetch_plan_limits_from_db(),
        ttl=300,
    )

Degrades gracefully when Redis is unavailable — falls back to direct DB read.
"""
import json
import os
from functools import wraps
from typing import Any, Callable, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
_CACHE_ENABLED = bool(os.environ.get("REDIS_URL")) or True  # enable if redis service is present

_redis_pool = None


async def _get_redis():
    global _redis_pool
    if not _CACHE_ENABLED:
        return None
    try:
        import redis.asyncio as aioredis
    except ImportError:
        return None
    if _redis_pool is None:
        try:
            _redis_pool = aioredis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
            await _redis_pool.ping()
        except Exception as e:
            logger.warning("redis_unavailable", error=str(e))
            _redis_pool = None
            return None
    return _redis_pool


async def get(key: str) -> Optional[str]:
    r = await _get_redis()
    if r is None:
        return None
    try:
        return await r.get(key)
    except Exception as e:
        logger.warning("redis_get_failed", key=key, error=str(e))
        return None


async def set(key: str, value: str, ttl: int = 300) -> bool:
    r = await _get_redis()
    if r is None:
        return False
    try:
        await r.setex(key, ttl, value)
        return True
    except Exception as e:
        logger.warning("redis_set_failed", key=key, error=str(e))
        return False


async def delete(key: str) -> bool:
    r = await _get_redis()
    if r is None:
        return False
    try:
        await r.delete(key)
        return True
    except Exception as e:
        logger.warning("redis_delete_failed", key=key, error=str(e))
        return False


async def get_or_set(key: str, factory: Callable[[], Any], ttl: int = 300) -> Any:
    """Get from cache or call factory, cache result, return it."""
    cached = await get(key)
    if cached is not None:
        try:
            return json.loads(cached)
        except (json.JSONDecodeError, TypeError):
            pass
    value = await factory() if asyncio.iscoroutinefunction(factory) else factory()
    await set(key, json.dumps(value, default=str), ttl=ttl)
    return value


def cached(ttl: int = 300):
    """Decorator: cache async function result in Redis for TTL seconds."""
    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            # Build a cache key from function name + args
            key_parts = [fn.__name__]
            key_parts.extend(str(a) for a in args)
            key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
            cache_key = ":".join(key_parts)
            return await get_or_set(cache_key, lambda: fn(*args, **kwargs), ttl=ttl)
        return wrapper
    return decorator


import asyncio
