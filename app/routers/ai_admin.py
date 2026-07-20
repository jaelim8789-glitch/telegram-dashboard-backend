"""
AI Employee Admin Routes — StyleProfile / 예약 메시지 / 커스텀 명령어 관리 API.

의존성:
  - app/bot/db.py — AI Employee 테이블 CRUD
  - app/api/deps.py — 관리자 인증 (원래 TeleMon/backend/auth_middleware.require_admin_user
    였으나, 이 저장소의 인증 체계로 교체 — 아래 함수들은 인증된 사용자 정보 자체는 쓰지 않고
    게이트로만 사용하므로 1:1 교체로 충분함)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_admin
from app.bot import db as bot_db
from app.production_config import get_config

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/bot/ai", tags=["ai-employee-admin"])


# ── StyleProfile CRUD ────────────────────────────────────────────────


@router.get("/style-profile/{chat_id}")
async def get_style_profile(
    chat_id: int,
    _admin: None = Depends(require_admin),
):
    """Get a group's AI style profile configuration."""
    bot_db.init_ai_tables()
    profile = bot_db.get_group_style_profile(chat_id)
    if not profile:
        return {
            "chat_id": chat_id,
            "style_profile_id": None,
            "available_actions": [
                "번역", "translate", "요약", "summarize",
                "날씨", "weather", "뉴스", "news", "도움말", "help",
            ],
            "configured": False,
        }
    profile["configured"] = True
    return profile


@router.post("/style-profile/{chat_id}")
async def set_style_profile(
    chat_id: int,
    body: dict,
    _admin: None = Depends(require_admin),
):
    """Set a group's AI style profile.

    Body:
        style_profile_id (str): 스타일 프로필 ID.
        available_actions (list[str], optional): 사용 가능한 액션 목록.
    """
    bot_db.init_ai_tables()
    style_profile_id = body.get("style_profile_id", "default")
    available_actions = body.get("available_actions")

    if not isinstance(style_profile_id, str) or not style_profile_id.strip():
        raise HTTPException(status_code=400, detail="style_profile_id must be a non-empty string")

    bot_db.upsert_group_style_profile(
        chat_id=chat_id,
        style_profile_id=style_profile_id.strip(),
        available_actions=available_actions,
    )

    logger.info("[ai_admin] style profile set for chat %s: %s", chat_id, style_profile_id)
    return {
        "chat_id": chat_id,
        "style_profile_id": style_profile_id,
        "available_actions": available_actions,
        "updated": True,
    }


@router.delete("/style-profile/{chat_id}")
async def delete_style_profile(
    chat_id: int,
    _admin: None = Depends(require_admin),
):
    """Delete a group's AI style profile (reset to default)."""
    bot_db.init_ai_tables()
    deleted = bot_db.delete_group_style_profile(chat_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Style profile not found")
    logger.info("[ai_admin] style profile deleted for chat %s", chat_id)
    return {"chat_id": chat_id, "deleted": True}


# ── Scheduled Messages ────────────────────────────────────────────────


@router.get("/scheduled-messages")
async def list_scheduled_messages(
    status: str | None = None,
    limit: int = 100,
    _admin: None = Depends(require_admin),
):
    """List AI scheduled messages, optionally filtered by status.

    Query params:
        status (str, optional): "pending" | "sent" | "failed" | "cancelled"
        limit (int, optional): 최대 결과 수 (기본 100).
    """
    bot_db.init_ai_tables()
    if status and status not in ("pending", "sent", "failed", "cancelled"):
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    messages = bot_db.get_scheduled_messages(status=status, limit=limit)
    return {"messages": messages, "total": len(messages)}


@router.post("/scheduled-messages/{msg_id}/cancel")
async def cancel_scheduled_message(
    msg_id: str,
    _admin: None = Depends(require_admin),
):
    """Cancel a pending scheduled message."""
    bot_db.init_ai_tables()
    cancelled = bot_db.cancel_scheduled_message(msg_id)
    if not cancelled:
        raise HTTPException(
            status_code=404,
            detail="Scheduled message not found or already sent/cancelled",
        )
    logger.info("[ai_admin] scheduled message %s cancelled by admin", msg_id)
    return {"message_id": msg_id, "cancelled": True}


# ── Health ────────────────────────────────────────────────────────────


@router.get("/health")
async def ai_employee_health(_admin: None = Depends(require_admin)):
    """AI Employee 시스템 상태 확인."""
    cfg = get_config().telegram_bot
    return {
        "configured": cfg.is_configured() if hasattr(cfg, "is_configured") else bool(cfg.bot_token),
        "tables_initialized": True,
    }
