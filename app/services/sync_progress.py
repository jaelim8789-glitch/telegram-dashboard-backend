"""Redis-backed sync progress tracking for Telegram accounts.

Design (per Epic 19):
  - Progress lives ONLY in Redis (key `sync:{account_id}`) with a 10-minute TTL
    that refreshes on every progress update. No DB writes for progress values
    (they churn constantly; writing them to Postgres would be wasted I/O).
  - On completion or failure the key is deleted immediately.
  - Only the terminal facts (last_sync_at, dialogs_count, messages_count) are
    written to the DB (via the caller updating the Account row).

SessionManager / the existing auth flow call into this module; the UI polls the
account + sync-progress endpoints.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.logging import get_logger
from app.database import async_session_maker

logger = get_logger(__name__)

PROGRESS_TTL_SECONDS = 600  # 10 min
_KEY_PREFIX = "sync:"


def _key(account_id: str) -> str:
    return f"{_KEY_PREFIX}{account_id}"


async def _redis() -> Any | None:
    from app.cache import _get_redis

    try:
        return await _get_redis()
    except Exception as e:
        logger.debug("sync_progress_redis_unavailable", error=str(e))
        return None


async def set_progress(
    account_id: str,
    *,
    stage: str,
    percent: int,
    dialogs: int | None = None,
    messages: int | None = None,
) -> None:
    """Record progress for an account's sync. Refreshes the 10-min TTL."""
    r = await _redis()
    if r is None:
        return
    key = _key(account_id)
    payload = {
        "stage": stage,
        "percent": max(0, min(100, int(percent))),
        "dialogs": int(dialogs or 0),
        "messages": int(messages or 0),
        "updated_at": int(asyncio.get_event_loop().time()) if False else None,
    }
    # updated_at wall-clock
    import time as _time
    payload["updated_at"] = int(_time.time())
    try:
        await r.set(key, __import__("json").dumps(payload), ex=PROGRESS_TTL_SECONDS)
    except Exception as e:
        logger.debug("sync_progress_set_error", account_id=account_id, error=str(e))


async def complete_sync(
    account_id: str,
    *,
    dialogs: int,
    messages: int,
    last_sync_at=None,
) -> None:
    """Mark sync finished: persist terminal facts to the DB, delete the Redis key."""
    import time as _time
    from datetime import datetime

    from app.models.account import Account
    from app.crud import account as account_crud

    # Persist terminal facts only.
    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is not None:
            account.dialog_count = int(dialogs)
            account.message_count = int(messages)
            from app.core.time import utcnow_naive
            account.last_sync_at = last_sync_at or utcnow_naive()
            await db.commit()

    r = await _redis()
    if r is not None:
        try:
            await r.delete(_key(account_id))
        except Exception as e:
            logger.debug("sync_progress_del_error", account_id=account_id, error=str(e))

    logger.info("account_sync_complete", account_id=account_id, dialogs=dialogs, messages=messages)


async def fail_sync(account_id: str, reason: str) -> None:
    """Mark sync failed: delete the Redis key so the queue slot clears."""
    r = await _redis()
    if r is not None:
        try:
            await r.delete(_key(account_id))
        except Exception as e:
            logger.debug("sync_progress_fail_del_error", account_id=account_id, error=str(e))
    logger.warning("account_sync_failed", account_id=account_id, reason=reason)


async def get_progress(account_id: str) -> dict | None:
    """Return current progress dict for a single account, or None."""
    r = await _redis()
    if r is None:
        return None
    try:
        raw = await r.get(_key(account_id))
    except Exception as e:
        logger.debug("sync_progress_get_error", account_id=account_id, error=str(e))
        return None
    if not raw:
        return None
    try:
        data = __import__("json").loads(raw)
    except Exception:
        return None
    data["account_id"] = account_id
    return data
