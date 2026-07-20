"""
Guest Mode Routes — TeleMon Bot의 Guest Mode 설정 및 통계 API.

Bot API 10.0+ (May 2026) Guest Mode 를 관리합니다:
  - @TeleMonBot Guest Mode 사용 통계 조회
  - 일일 무료 사용 한도 설정
  - Webhook allowed_updates 갱신 (guest_message 활성화)

의존성:
  - backend/bot/guest_engine.py — GuestEngine
  - backend/bot/telegram_api.py — TelegramBotClient (answerGuestQuery)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_admin
from app.bot import service as bot_service
from app.bot.telegram_api import TelegramBotClient
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/bot/guest", tags=["guest-mode"])


def _require_engine():
    cfg = settings
    if not getattr(cfg, "telegram_bot_token", None):
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN not configured")

    client = TelegramBotClient(cfg.telegram_bot_token)
    engine = bot_service._get_guest_engine(client)
    if engine is None:
        raise HTTPException(status_code=503, detail="GuestEngine not initialized")
    return engine


@router.get("/stats")
async def guest_stats(_user: dict = Depends(require_admin)):
    """Guest Mode 사용 통계 조회 (관리자 전용).

    Returns:
        enabled: bool — Guest Mode 활성화 여부
        daily_limit: int — 사용자별 일일 무료 사용 한도
        unique_users_today: int — 오늘 방문한 고유 사용자 수
        total_requests_today: int — 오늘 총 요청 수
        daily_usage: dict — 사용자별 사용량 (user_id -> count)
    """
    engine = _require_engine()
    return {
        "enabled": True,
        "daily_limit": engine.daily_limit,
        "unique_users_today": engine.unique_users_today,
        "total_requests_today": engine.total_requests_today,
        "daily_usage": engine.daily_usage_snapshot,
    }


@router.post("/daily-limit")
async def set_daily_limit(
    limit: int,
    _user: dict = Depends(require_admin),
):
    """일일 무료 사용 한도 설정 (관리자 전용).

    Args:
        limit: 새 한도 값 (1 이상)
    """
    if limit < 1:
        raise HTTPException(status_code=400, detail="Limit must be >= 1")

    engine = _require_engine()
    engine.daily_limit = limit

    logger.info("[guest] daily limit updated to %d by admin", limit)
    return {"daily_limit": limit, "updated": True}


@router.post("/user-limit")
async def set_user_limit(
    body: dict,
    _user: dict = Depends(require_admin),
):
    """사용자별 일일 한도 설정. body.user_id: str, body.limit: int (1 이상)

    특정 사용자의 한도를 글로벌 daily_limit과 다르게 설정합니다.
    limit을 0으로 설정하면 사용자별 한도를 제거하고 글로벌 설정을 따릅니다.
    """
    user_id = body.get("user_id", "")
    limit = body.get("limit", 0)
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    engine = _require_engine()
    if limit < 1:
        # Remove per-user override
        engine._user_limits.pop(user_id, None)
        logger.info("[guest] user limit removed for %s (falls back to global %d)", user_id, engine.daily_limit)
        return {"user_id": user_id, "limit": None, "effective_limit": engine.daily_limit}
    else:
        engine._user_limits[user_id] = limit
        logger.info("[guest] user limit set: %s -> %d", user_id, limit)
        return {"user_id": user_id, "limit": limit, "effective_limit": limit}


@router.post("/webhook-refresh")
async def refresh_webhook(_user: dict = Depends(require_admin)):
    cfg = settings
    if not getattr(cfg, "telegram_bot_token", None) or not getattr(cfg, "telegram_webhook_url", None):
        raise HTTPException(status_code=503, detail="Bot token or webhook URL not configured")

    client = TelegramBotClient(cfg.telegram_bot_token)
    try:
        result = await client.set_webhook(
            url=cfg.webhook_url,
            secret_token=cfg.webhook_secret or None,
            allowed_updates=[
                "message",
                "callback_query",
                "guest_message",
            ],
        )
        logger.info("[guest] webhook refreshed for guest_message updates")
        return {"ok": True, "result": result}
    except Exception as e:
        logger.error("[guest] webhook refresh failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Webhook refresh failed: {e}")


@router.get("/webhook-info")
async def webhook_info(_user: dict = Depends(require_admin)):
    cfg = settings
    if not getattr(cfg, "telegram_bot_token", None):
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN not configured")

    client = TelegramBotClient(cfg.telegram_bot_token)
    try:
        info = await client.get_webhook_info()
        return info
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
