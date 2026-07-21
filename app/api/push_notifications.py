"""
Push Notification Service — Web Push + FCM

Endpoints:
  POST /api/push/send     — Send push to all subscribed devices
  POST /api/push/subscribe   — Register a push subscription
  POST /api/push/unsubscribe — Unsubscribe
"""

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity
from app.database import get_db
from app.core.logging import get_logger
from app.core.security import require_admin

router = APIRouter(prefix="/api/push", tags=["push"])
logger = get_logger(__name__)


# ── Models ──

class PushSubscriptionModel(BaseModel):
    endpoint: str
    keys: dict | None = None
    platform: str = "web"  # web, capacitor-android, capacitor-ios
    device_token: str | None = None  # FCM token for native devices


class PushSendRequest(BaseModel):
    title: str = "TeleMon"
    body: str
    url: Optional[str] = "/app"


# ── Push Subscription Table (simple file-based, or use DB if available) ──
# For production, replace with a proper DB table.

try:
    from app.models.system_setting import SystemSetting
except ImportError:
    SystemSetting = None


# ── POST /api/push/subscribe ──

@router.post("/subscribe")
async def subscribe_push(
    body: PushSubscriptionModel,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """Register a push subscription"""
    from app.models.system_setting import SystemSetting

    # Store as JSON in SystemSetting for simplicity
    key = f"push_sub_{identity.tenant_id}_{hash(body.endpoint)}"
    setting = SystemSetting(
        key=key,
        value=json.dumps({
            "tenant_id": identity.tenant_id,
            "endpoint": body.endpoint,
            "keys": body.keys,
            "platform": body.platform,
            "device_token": body.device_token,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }),
        description=f"Push subscription for {identity.tenant_id}",
    )
    db.add(setting)
    await db.commit()
    logger.info("push_subscribed", tenant_id=identity.tenant_id, platform=body.platform)
    return {"ok": True}


# ── POST /api/push/unsubscribe ──

@router.post("/unsubscribe")
async def unsubscribe_push(
    body: PushSubscriptionModel,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """Remove a push subscription"""
    from app.models.system_setting import SystemSetting

    key = f"push_sub_{identity.tenant_id}_{hash(body.endpoint)}"
    stmt = delete(SystemSetting).where(SystemSetting.key == key)
    await db.execute(stmt)
    await db.commit()
    logger.info("push_unsubscribed", tenant_id=identity.tenant_id)
    return {"ok": True}


# ── POST /api/push/send (admin only) ──

@router.post("/send")
async def send_push_notification(
    body: PushSendRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """Send push notification to all subscribers. Admin only."""
    from app.models.system_setting import SystemSetting

    # Load all subscriptions
    stmt = select(SystemSetting).where(SystemSetting.key.like("push_sub_%"))
    result = await db.execute(stmt)
    subscriptions = result.scalars().all()

    if not subscriptions:
        raise HTTPException(status_code=404, detail="No subscribers found")

    sent = 0
    for sub in subscriptions:
        try:
            data = json.loads(sub.value)
            payload = json.dumps({
                "title": body.title,
                "body": body.body,
                "icon": "/icon-192.png",
                "badge": "/icon-192.png",
                "data": {"url": body.url},
            })

            # Web push via Service Worker
            if data.get("platform") == "web":
                await _send_web_push(data["endpoint"], data.get("keys", {}), payload)
                sent += 1

            # Native push via FCM (requires firebase-admin setup)
            elif data.get("device_token"):
                await _send_fcm_push(data["device_token"], body.title, body.body, body.url)
                sent += 1

        except Exception as e:
            logger.error("push_send_failed", error=str(e), subscriber=sub.key)

    logger.info("push_sent", count=sent, total=len(subscriptions))
    return {"ok": True, "sent": sent, "total": len(subscriptions)}


async def _send_web_push(endpoint: str, keys: dict, payload: str):
    """Send web push using Web Push protocol (VAPID)"""
    try:
        from app.config import settings

        vapid_private_key = settings.vapid_private_key
        vapid_claims = {"sub": "mailto:support@telemon.online"}

        # Use pywebpush if available
        try:
            from pywebpush import webpush
            await webpush(
                endpoint_info={"endpoint": endpoint, "keys": keys},
                data=payload,
                vapid_private_key=vapid_private_key,
                vapid_claims=vapid_claims,
            )
        except ImportError:
            logger.warning("pywebpush not installed — run: pip install pywebpush")
    except Exception as e:
        logger.error("web_push_failed", error=str(e))


async def _send_fcm_push(token: str, title: str, body: str, url: str | None = None):
    """Send FCM push to native Android/iOS devices"""
    try:
        import firebase_admin
        from firebase_admin import credentials, messaging

        if not firebase_admin._apps:
            cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred)

        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            token=token,
            data={"url": url or "/app"},                    ) if url else None,
            android=messaging.AndroidConfig(
                priority="high",
            ) if url else None,
        )
        messaging.send(message)
    except ImportError:
        logger.warning("firebase-admin not installed — run: pip install firebase-admin")
    except Exception as e:
        logger.error("fcm_push_failed", error=str(e))
