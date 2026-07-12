"""In-memory per-IP rate limiter for authentication endpoints.

Process-local only — state resets on server restart.
Not a distributed rate-limiting platform.

Strategy: sliding-window counter keyed by (client_ip, category).
Each category has its own independent counter so one endpoint
cannot consume another's budget.

No secrets (passwords, API keys) appear in limiter keys or logs.
"""

import time

from fastapi import Request

from app.core.logging import get_logger

logger = get_logger(__name__)

# { (ip, category): [timestamp, ...] }
_window: dict[tuple[str, str], list[float]] = {}


def _prune(category_key: tuple[str, str], window_seconds: float) -> list[float]:
    """Return timestamps still within the window, pruning stale entries."""
    now = time.time()
    cutoff = now - window_seconds
    timestamps = _window.get(category_key, [])
    active = [ts for ts in timestamps if ts > cutoff]
    if active:
        _window[category_key] = active
    else:
        _window.pop(category_key, None)
    return active


def check_rate_limit(
    client_ip: str,
    category: str,
    max_attempts: int = 10,
    window_seconds: float = 300.0,
) -> bool:
    """Returns True if the request is allowed, False if rate-limited."""
    key = (client_ip, category)
    active = _prune(key, window_seconds)

    if len(active) >= max_attempts:
        logger.warning("rate_limit_exceeded", category=category, ip=client_ip)
        return False

    _window.setdefault(key, []).append(time.time())
    return True


def get_retry_after_seconds(client_ip: str, category: str, window_seconds: float = 300.0) -> int:
    """Return seconds until the oldest entry in the window expires."""
    key = (client_ip, category)
    timestamps = _window.get(key, [])
    if not timestamps:
        return 0
    now = time.time()
    cutoff = now - window_seconds
    for ts in timestamps:
        remaining = ts + window_seconds - now
        if remaining > 0:
            return int(remaining) + 1
    return 0


def reset_rate_limits() -> None:
    """Clear all rate-limit state. Used in tests between cases."""
    _window.clear()


def reset_rate_limit_for_ip(client_ip: str) -> None:
    """Clear rate-limit state for a specific IP. Used in tests."""
    keys = [k for k in _window if k[0] == client_ip]
    for k in keys:
        _window.pop(k, None)


def get_client_ip(request: Request) -> str:
    """Return the real client IP when behind our trusted nginx proxy.

    We trust X-Real-IP set by our own nginx (configured to $remote_addr —
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
