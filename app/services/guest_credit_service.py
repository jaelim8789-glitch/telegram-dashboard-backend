"""Guest AI credit bucket, keyed by a per-device identifier.

Guests (no login, no tenant) get a per-device credit balance instead of the
old fixed "30 messages / day" rate limit. 1 credit = 1 character (input +
output combined), same billing rule as member chat.

The identifier is normally the browser cookie `guest_device_id` issued by
the guest chat endpoints, so two visitors behind the same NAT/corporate IP
no longer share one bucket. When no cookie is present (curl, tests, first
request before the cookie round-trips) the client IP is used as a stable
fallback so the bucket still exists.

State lives in Redis so it survives worker restarts and holds across
uvicorn workers; falls back to an in-memory dict when Redis is unavailable
(never a hard dependency).
"""

from __future__ import annotations

import os
import threading
import time

from app.core.logging import get_logger

logger = get_logger(__name__)

GUEST_CREDITS_PER_REFILL = 30_000
GUEST_REFILL_INTERVAL_SECONDS = 3 * 60 * 60  # 3 hours

# ─── In-memory fallback state ───────────────────────────────────────────
# { identifier: {"remaining": int, "last_refill_at": float} }
_mem: dict[str, dict] = {}
_mem_lock = threading.Lock()


def _get_redis():
    """Lazily build a sync redis client. Returns None when unavailable."""
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
        logger.debug("guest_credit_redis_unavailable", error=str(e))
        return None


def _key(identifier: str) -> str:
    return f"guest_credits:{identifier}"


def _maybe_refill(remaining: int, last_refill_at: float | None) -> tuple[int, float, bool]:
    """Return (remaining, last_refill_at, did_refill)."""
    now = time.time()
    if remaining >= GUEST_CREDITS_PER_REFILL:
        return remaining, last_refill_at or now, False
    if last_refill_at is None:
        return GUEST_CREDITS_PER_REFILL, now, True
    if now - last_refill_at >= GUEST_REFILL_INTERVAL_SECONDS:
        return GUEST_CREDITS_PER_REFILL, now, True
    return remaining, last_refill_at, False


def get_guest_credits(identifier: str) -> tuple[int, float | None]:
    """Return (remaining, last_refill_at) with inline refill for the identifier."""
    client = _get_redis()
    if client is not None:
        try:
            key = _key(identifier)
            pipe = client.pipeline()
            pipe.get(key)
            pipe.get(f"{key}:refill")
            remaining_raw, refill_raw = pipe.execute()
            remaining = int(remaining_raw) if remaining_raw is not None else GUEST_CREDITS_PER_REFILL
            last_refill = float(refill_raw) if refill_raw is not None else None
            remaining, last_refill, refilled = _maybe_refill(remaining, last_refill)
            if refilled:
                pipe2 = client.pipeline()
                pipe2.set(key, remaining)
                pipe2.set(f"{key}:refill", last_refill)
                pipe2.execute()
            elif remaining_raw is None:
                client.set(key, remaining)
                client.set(f"{key}:refill", last_refill or time.time())
            return remaining, last_refill
        except Exception as e:
            logger.debug("guest_credit_redis_read_error", error=str(e))

    with _mem_lock:
        entry = _mem.get(identifier)
        if entry is None:
            entry = {"remaining": GUEST_CREDITS_PER_REFILL, "last_refill_at": time.time()}
            _mem[identifier] = entry
        entry["remaining"], entry["last_refill_at"], refilled = _maybe_refill(
            entry["remaining"], entry.get("last_refill_at")
        )
        return entry["remaining"], entry.get("last_refill_at")


def try_deduct_guest_credits(identifier: str, amount: int) -> tuple[bool, int]:
    """Deduct *amount* credits from the identifier's bucket. Returns (ok, remaining)."""
    if amount <= 0:
        return True, get_guest_credits(identifier)[0]
    client = _get_redis()
    if client is not None:
        try:
            key = _key(identifier)
            remaining_raw = client.get(key)
            remaining = int(remaining_raw) if remaining_raw is not None else GUEST_CREDITS_PER_REFILL
            last_refill = float(client.get(f"{key}:refill") or 0) or None
            remaining, last_refill, refilled = _maybe_refill(remaining, last_refill)
            if remaining < amount:
                return False, remaining
            remaining -= amount
            pipe = client.pipeline()
            pipe.set(key, remaining)
            pipe.set(f"{key}:refill", last_refill or time.time())
            pipe.execute()
            return True, remaining
        except Exception as e:
            logger.debug("guest_credit_redis_deduct_error", error=str(e))

    with _mem_lock:
        entry = _mem.get(identifier)
        if entry is None:
            entry = {"remaining": GUEST_CREDITS_PER_REFILL, "last_refill_at": time.time()}
            _mem[identifier] = entry
        entry["remaining"], entry["last_refill_at"], _ = _maybe_refill(
            entry["remaining"], entry.get("last_refill_at")
        )
        if entry["remaining"] < amount:
            return False, entry["remaining"]
        entry["remaining"] -= amount
        return True, entry["remaining"]


def guest_refill_countdown_seconds(identifier: str) -> int:
    """Seconds until the next refill (0 if the bucket is already full)."""
    remaining, last_refill_at = get_guest_credits(identifier)
    if remaining >= GUEST_CREDITS_PER_REFILL or last_refill_at is None:
        return 0
    elapsed = time.time() - last_refill_at
    return max(0, int(GUEST_REFILL_INTERVAL_SECONDS - elapsed))


def reset_guest_credits_for_ip(identifier: str) -> None:
    """Reset an identifier's bucket to full. Used by tests."""
    client = _get_redis()
    if client is not None:
        try:
            client.delete(_key(identifier), f"{_key(identifier)}:refill")
        except Exception:
            pass
    with _mem_lock:
        _mem.pop(identifier, None)
