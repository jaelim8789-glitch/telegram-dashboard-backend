"""Per-IP rate limiter for authentication endpoints.

Primary strategy: distributed fixed-window counter in Redis (INCR + EXPIRE)
so limits hold across uvicorn workers (with UVICORN_WORKERS>1 the old
in-memory counters were per-process, letting an attacker multiply their
budget by the worker count).

Fallback strategy: when Redis is unavailable (tests, degraded deploys) we
fall back to the previous sliding-window in-memory counter so the limiter
never becomes a hard dependency.

Implementation note: the Redis client here is the *sync* redis-py client
(`redis.Redis`), not the asyncio one used by app/cache.py. check_rate_limit
is a synchronous function that is called from inside async FastAPI deps, so
`asyncio.run()` would fail inside the running loop; a short sync INCR+EXPIRE
(~1ms) is the pragmatic, correct choice here.

No secrets (passwords, API keys) appear in limiter keys or logs.
"""

import os
import threading
import time

from fastapi import Request

from app.core.logging import get_logger

logger = get_logger(__name__)

# ─── In-memory fallback state ───────────────────────────────────────────
# { (ip, category): [timestamp, ...] }
_window: dict[tuple[str, str], list[float]] = {}
_window_lock = threading.Lock()
_MAX_ENTRIES = 10_000
_CLEANUP_INTERVAL_SECONDS = 300.0

# Whether Redis is usable. Cached briefly so a healthy Redis doesn't add a
# probe to every request, and a downed Redis re-probes occasionally instead
# of staying permanently disabled.
_redis_usable: bool | None = None
_redis_checked_at: float = 0.0
_REDIS_RECHECK_SECONDS = 30.0


def _prune(category_key: tuple[str, str], window_seconds: float) -> list[float]:
    """Return timestamps still within the window, pruning stale entries.

    Caller MUST hold _window_lock.
    """
    now = time.time()
    cutoff = now - window_seconds
    timestamps = _window.get(category_key, [])
    active = [ts for ts in timestamps if ts > cutoff]
    if active:
        _window[category_key] = active
    else:
        _window.pop(category_key, None)
    return active


def _periodic_cleanup() -> None:
    """Remove all entries whose timestamps are older than window_seconds."""
    with _window_lock:
        now = time.time()
        keys_to_expire: list[tuple[str, str]] = []
        for key, timestamps in list(_window.items()):
            if not timestamps:
                keys_to_expire.append(key)
                continue
            cutoff = now - max(ts for ts in timestamps) - 1.0
            if timestamps[-1] < cutoff:
                keys_to_expire.append(key)
        for key in keys_to_expire:
            _window.pop(key, None)
        removed = len(keys_to_expire)
        remaining = len(_window)
    logger.debug("rate_limiter_cleanup", removed=removed, remaining=remaining)
    _schedule_cleanup()


def _schedule_cleanup() -> None:
    """Schedule cleanup without keeping short-lived CLI processes alive."""
    timer = threading.Timer(_CLEANUP_INTERVAL_SECONDS, _periodic_cleanup)
    timer.daemon = True
    timer.start()


_periodic_cleanup()


# ─── Redis backend ──────────────────────────────────────────────────────


def _get_redis_client():
    """Lazily build a sync redis-py client from REDIS_URL.

    Returns None when REDIS_URL is unset, redis-py isn't installed, or the
    first connection attempt fails. Mirrors app/cache.py's graceful-degradation
    contract (Redis absent => feature degrades, never crashes).
    """
    if not os.environ.get("REDIS_URL"):
        return None
    try:
        import redis as _redis_sync
    except ImportError:
        return None
    try:
        client = _redis_sync.Redis.from_url(
            os.environ["REDIS_URL"],
            decode_responses=True,
            socket_connect_timeout=1.5,
            socket_timeout=1.5,
        )
        client.ping()
        return client
    except Exception as e:
        logger.debug("rate_limiter_redis_unavailable", error=str(e))
        return None


def _redis_usable_now() -> bool:
    """Return True when the shared Redis connection is usable (cached probe)."""
    global _redis_usable, _redis_checked_at
    now = time.time()
    if _redis_usable is not None and (now - _redis_checked_at) < _REDIS_RECHECK_SECONDS:
        return _redis_usable
    client = _get_redis_client()
    _redis_usable = client is not None
    _redis_checked_at = now
    return bool(_redis_usable)


def _check_rate_limit_redis(client_ip: str, category: str, max_attempts: int, window_seconds: float) -> bool | None:
    """Distributed fixed-window counter: INCR + EXPIRE on rl:{category}:{ip}.

    Returns True if allowed, False if rate-limited, None if Redis is not
    usable (caller should fall back to the in-memory counter).
    """
    try:
        import redis as _redis_sync
    except ImportError:
        return None
    try:
        client = _redis_sync.Redis.from_url(
            os.environ.get("REDIS_URL", ""),
            decode_responses=True,
            socket_connect_timeout=1.5,
            socket_timeout=1.5,
        )
        if not client.ping():
            return None
        key = f"rl:{category}:{client_ip}"
        count = client.incr(key)
        if count == 1:
            client.expire(key, int(window_seconds))
        return count <= max_attempts
    except Exception as e:
        logger.warning("rate_limiter_redis_error", category=category, error=str(e))
        return None


def check_rate_limit(
    client_ip: str,
    category: str,
    max_attempts: int = 10,
    window_seconds: float = 300.0,
) -> bool:
    """Returns True if the request is allowed, False if rate-limited.

    Uses the distributed Redis counter when Redis is reachable, otherwise
    falls back to the in-memory sliding-window counter.
    """
    if _redis_usable_now():
        result = _check_rate_limit_redis(client_ip, category, max_attempts, window_seconds)
        if result is not None:
            if not result:
                logger.warning("rate_limit_exceeded", category=category, ip=client_ip)
            return result
        # Redis probe said usable but the request failed -> fall through.
    return _check_rate_limit_memory(client_ip, category, max_attempts, window_seconds)


def _check_rate_limit_memory(
    client_ip: str,
    category: str,
    max_attempts: int = 10,
    window_seconds: float = 300.0,
) -> bool:
    """In-memory sliding-window fallback (original behavior)."""
    key = (client_ip, category)
    with _window_lock:
        active = _prune(key, window_seconds)

        if len(active) >= max_attempts:
            logger.warning("rate_limit_exceeded", category=category, ip=client_ip)
            return False

        if len(_window) >= _MAX_ENTRIES and key not in _window:
            logger.warning("rate_limiter_at_capacity", ip=client_ip, category=category)
            return False

        _window.setdefault(key, []).append(time.time())
    return True


def get_retry_after_seconds(client_ip: str, category: str, window_seconds: float = 300.0) -> int:
    """Return seconds until the oldest entry in the window expires."""
    if _redis_usable_now():
        try:
            import redis as _redis_sync
            client = _redis_sync.Redis.from_url(
                os.environ.get("REDIS_URL", ""),
                decode_responses=True,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
            )
            if client.ping():
                ttl = client.ttl(f"rl:{category}:{client_ip}")
                return max(int(ttl), 0)
        except Exception as e:
            logger.warning("rate_limiter_ttl_error", error=str(e))
            return 0

    key = (client_ip, category)
    with _window_lock:
        timestamps = _window.get(key, [])
        if not timestamps:
            return 0
        now = time.time()
        for ts in timestamps:
            remaining = ts + window_seconds - now
            if remaining > 0:
                return int(remaining) + 1
    return 0


def reset_rate_limits() -> None:
    """Clear all rate-limit state. Used in tests between cases."""
    with _window_lock:
        _window.clear()


def reset_rate_limit_for_ip(client_ip: str) -> None:
    """Clear rate-limit state for a specific IP. Used in tests."""
    with _window_lock:
        keys = [k for k in _window if k[0] == client_ip]
        for k in keys:
            _window.pop(k, None)


def get_client_ip(request: Request) -> str:
    """Return the real client IP when behind our trusted nginx proxy.

    We trust X-Real-IP set by our own nginx (configured to $remote_addr 
    the direct TCP peer, not spoofable by the client). We do NOT blindly
    trust X-Forwarded-For headers from arbitrary clients because that
    header can be trivially set to anything.

    In test/development (no nginx), falls back to request.client.host.
    """
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return "unknown"
