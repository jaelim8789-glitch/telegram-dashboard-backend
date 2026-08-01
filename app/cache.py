"""Redis caching layer for frequently-read, infrequently-changed data.

Usage:
    from app.cache import redis_cache

    # Cache plan limits for 5 minutes
    limits = await redis_cache.get_or_set(
        "plan_limits",
        lambda: fetch_plan_limits_from_db(),
        ttl=300,
    )

Degrades gracefully when Redis is unavailable  falls back to direct DB read.
"""
import asyncio
import json
import os
from functools import wraps
from typing import Any, Callable, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
_CACHE_ENABLED = bool(os.environ.get("REDIS_URL"))

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


_singleton_renewal_tasks: dict[str, "asyncio.Task"] = {}


async def acquire_singleton_lock(name: str, ttl: int = 30) -> bool:
    """True if this process won the right to run a per-container singleton
    (a background scheduler, a poller, ...) under `uvicorn --workers N`, where
    every worker's startup hook runs independently. Only the first worker to
    call this for a given name gets True; the rest get False and should skip
    starting that singleton entirely rather than run N duplicate copies of it.

    The caller holds this lock for the process's entire lifetime (it's never
    re-acquired), so a winning call also starts a background task that renews
    the key every ttl/3 seconds. Without renewal, the key — needed with a
    short TTL so a crashed/killed holder's lock expires promptly — would also
    expire under a *still-running* holder (e.g. mid rolling-deploy, where the
    old and new containers briefly overlap), letting the new worker acquire
    the same singleton and run a duplicate poller/scheduler alongside it. For
    telegram_bot_polling specifically this is what produced the recurring
    "Conflict: terminated by other getUpdates request" log spam; for
    scheduler/auto_reply_listeners/ai_scheduler the same gap risks duplicate
    sends instead of just noise.

    Degrades to True (i.e. "go ahead and start it") when Redis is unavailable,
    matching this module's existing graceful-degradation behavior — falling
    back to the old un-guarded, occasionally-duplicated behavior beats not
    starting the singleton at all.
    """
    r = await _get_redis()
    if r is None:
        return True
    try:
        acquired = bool(await r.set(f"singleton_lock:{name}", os.getpid(), nx=True, ex=ttl))
    except Exception as e:
        logger.warning("redis_lock_failed", name=name, error=str(e))
        return True
    if acquired:
        _start_singleton_lock_renewal(name, ttl)
    return acquired


def _start_singleton_lock_renewal(name: str, ttl: int) -> None:
    async def _renew_loop() -> None:
        interval = max(1, ttl // 3)
        while True:
            await asyncio.sleep(interval)
            r = await _get_redis()
            if r is None:
                continue
            try:
                await r.expire(f"singleton_lock:{name}", ttl)
            except Exception as e:
                logger.warning("redis_lock_renew_failed", name=name, error=str(e))

    _singleton_renewal_tasks[name] = asyncio.create_task(_renew_loop())


async def release_singleton_lock(name: str) -> None:
    """Pair with acquire_singleton_lock for a clean shutdown: stops the
    renewal task and deletes the key immediately, instead of leaving it to
    expire on its own (and the task running forever re-touching a key that
    still exists past this process's intended lifetime)."""
    task = _singleton_renewal_tasks.pop(name, None)
    if task is not None:
        task.cancel()
    await delete(f"singleton_lock:{name}")


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
