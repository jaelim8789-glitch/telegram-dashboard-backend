"""Webhook notification service — sends HTTP callbacks to user-configured URLs.

Supports multiple webhook URLs per tenant, event-type filtering, and
automatic retry with exponential backoff for transient failures.

Events:
  - broadcast.completed  — a broadcast finished (success or partial failure)
  - broadcast.failed     — a broadcast fully failed
  - account.unauthorized — an account session expired / needs re-auth
  - account.banned       — an account was banned by Telegram
  - auto_reply.triggered — an auto-reply rule was triggered
  - macro.sent           — a reply macro was sent
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Literal

import httpx
from sqlalchemy import select

from app.core.logging import get_logger
from app.database import async_session_maker

logger = get_logger(__name__)

EventType = Literal[
    "broadcast.completed",
    "broadcast.failed",
    "account.unauthorized",
    "account.banned",
    "auto_reply.triggered",
    "macro.sent",
]

_WEBHOOK_TIMEOUT = 10  # seconds
_MAX_RETRIES = 2
_RETRY_DELAY_MS = 500

# In-memory webhook URL cache per tenant (refreshed on every send)
_webhook_cache: dict[str, list[str]] = {}
_webhook_cache_ttl: dict[str, float] = {}  # timestamp of last refresh
_WEBHOOK_CACHE_TTL_SECONDS = 300  # 5 minutes


async def _load_webhook_urls(tenant_id: str) -> list[str]:
    """Load active webhook URLs for a tenant from the database.

    Stored in a simple JSON column on the Tenant model or a separate
    webhook_configs table. Falls back to empty list if no config found.
    """
    # Check cache first (with TTL)
    if tenant_id in _webhook_cache:
        last_fetch = _webhook_cache_ttl.get(tenant_id, 0)
        if time.time() - last_fetch < _WEBHOOK_CACHE_TTL_SECONDS:
            return _webhook_cache[tenant_id]

    async with async_session_maker() as db:
        from app.models.tenant import Tenant
        result = await db.execute(
            select(Tenant.webhook_urls).where(Tenant.id == tenant_id)
        )
        raw = result.scalar_one_or_none()
        if raw:
            try:
                urls = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                urls = []
        else:
            urls = []
        _webhook_cache[tenant_id] = urls
        _webhook_cache_ttl[tenant_id] = time.time()
        return urls


async def _save_webhook_urls(tenant_id: str, urls: list[str]) -> None:
    """Persist webhook URLs for a tenant."""
    async with async_session_maker() as db:
        from app.models.tenant import Tenant
        result = await db.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        )
        tenant = result.scalar_one_or_none()
        if tenant:
            tenant.webhook_urls = json.dumps(urls, ensure_ascii=False)
            await db.commit()
    # Update cache
    _webhook_cache[tenant_id] = urls


def _invalidate_cache(tenant_id: str) -> None:
    """Invalidate cached webhook URLs for a tenant."""
    _webhook_cache.pop(tenant_id, None)
    _webhook_cache_ttl.pop(tenant_id, None)


async def get_webhook_urls(tenant_id: str) -> list[str]:
    """Get webhook URLs for a tenant (public API)."""
    return await _load_webhook_urls(tenant_id)


async def set_webhook_urls(tenant_id: str, urls: list[str]) -> None:
    """Set webhook URLs for a tenant (validates URLs first)."""
    validated = []
    for url in urls:
        url = url.strip()
        if url and (url.startswith("https://") or url.startswith("http://")):
            validated.append(url)
    await _save_webhook_urls(tenant_id, validated)


async def send_webhook(
    tenant_id: str,
    event: EventType,
    payload: dict,
) -> int:
    """Send a webhook event to all configured URLs for a tenant.

    Returns the number of successfully delivered webhooks (0..N).
    Silently ignores failures — the caller should not block on this.
    """
    urls = await _load_webhook_urls(tenant_id)
    if not urls:
        return 0

    body = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "data": payload,
    }

    success_count = 0
    async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT) as client:
        for url in urls:
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    response = await client.post(
                        url,
                        json=body,
                        headers={"Content-Type": "application/json", "User-Agent": "TeleMon-Webhook/1.0"},
                    )
                    if response.is_success:
                        success_count += 1
                        logger.info("webhook_delivered", tenant_id=tenant_id, url=url[:40], event=event)
                        break
                    elif attempt < _MAX_RETRIES:
                        await asyncio.sleep(_RETRY_DELAY_MS / 1000 * (attempt + 1))
                    else:
                        logger.warning(
                            "webhook_failed",
                            tenant_id=tenant_id,
                            url=url[:40],
                            event=event,
                            status=response.status_code,
                        )
                except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
                    if attempt < _MAX_RETRIES:
                        await asyncio.sleep(_RETRY_DELAY_MS / 1000 * (attempt + 1))
                    else:
                        logger.warning(
                            "webhook_connection_failed",
                            tenant_id=tenant_id,
                            url=url[:40],
                            event=event,
                            error=str(exc)[:100],
                        )

    return success_count


# ─── Convenience helpers for common events ────────────────────────────


async def notify_broadcast_completed(
    tenant_id: str,
    broadcast_id: str,
    message_preview: str,
    success_count: int,
    failure_count: int,
    total_recipients: int,
) -> int:
    """Notify that a broadcast has completed."""
    return await send_webhook(tenant_id, "broadcast.completed", {
        "broadcast_id": broadcast_id,
        "message_preview": message_preview[:100],
        "success_count": success_count,
        "failure_count": failure_count,
        "total_recipients": total_recipients,
        "status": "completed" if failure_count == 0 else "partial",
    })


async def notify_broadcast_failed(
    tenant_id: str,
    broadcast_id: str,
    message_preview: str,
    error: str,
) -> int:
    """Notify that a broadcast has fully failed."""
    return await send_webhook(tenant_id, "broadcast.failed", {
        "broadcast_id": broadcast_id,
        "message_preview": message_preview[:100],
        "error": error[:200],
    })


async def notify_account_unauthorized(
    tenant_id: str,
    account_id: str,
    phone: str,
    name: str | None,
) -> int:
    """Notify that an account session has expired."""
    return await send_webhook(tenant_id, "account.unauthorized", {
        "account_id": account_id,
        "phone": phone,
        "name": name,
    })


async def notify_account_banned(
    tenant_id: str,
    account_id: str,
    phone: str,
    name: str | None,
) -> int:
    """Notify that an account was banned by Telegram."""
    return await send_webhook(tenant_id, "account.banned", {
        "account_id": account_id,
        "phone": phone,
        "name": name,
    })


async def notify_auto_reply_triggered(
    tenant_id: str,
    rule_name: str,
    chat_title: str,
    trigger_message: str,
    reply_content: str,
) -> int:
    """Notify that an auto-reply was triggered."""
    return await send_webhook(tenant_id, "auto_reply.triggered", {
        "rule_name": rule_name,
        "chat_title": chat_title,
        "trigger_message": trigger_message[:100],
        "reply_content": reply_content[:100],
    })
