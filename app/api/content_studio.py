"""TeleMon AI Content Studio — auto-generated broadcast content."""

import random
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Identity, get_current_identity
from app.crud import broadcast as broadcast_crud
from app.crud.content_calendar import (
    create_content_calendar_setting,
    delete_content_calendar_setting,
    get_content_calendar_setting,
    list_content_calendar_settings,
    update_content_calendar_setting,
)
from app.database import get_db
from app.models.content_calendar import ContentCalendarSetting
from app.schemas.broadcast import BroadcastCreate
from app.schemas.content_studio import (
    ContentCalendarSettingCreate,
    ContentCalendarSettingRead,
    ContentCalendarSettingUpdate,
    ContentGenerateRequest,
    ContentGenerateResponse,
)
from app.services.ai_content_studio_service import (
    FEATURE_CONTENT_STUDIO,
    generate_content,
    get_random_content_type,
)
from app.services.ai_core_service import check_ai_quota

router = APIRouter(prefix="/api/ai/content-studio", tags=["ai-content-studio"])

logger = __import__("app.core.logging", fromlist=["get_logger"]).get_logger(__name__)


# ─── Generate ─────────────────────────────────────────────────────────────


@router.post("/generate", response_model=ContentGenerateResponse)
async def generate_content_studio(
    payload: ContentGenerateRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> ContentGenerateResponse:
    """Generate marketing content using AI.

    Accepts a content type and tone, calls DeepSeek, and returns the
    generated message along with token usage.
    """
    tenant_id = identity.tenant_id or "anonymous"

    allowed, reason = await check_ai_quota(db, tenant_id, FEATURE_CONTENT_STUDIO)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=reason)

    content, tokens, content_studio_content_id = await generate_content(
        content_type=payload.content_type,
        tone=payload.tone,
        topic=payload.topic,
        context=payload.context,
        tenant_id=tenant_id,
        style_profile_id=payload.style_profile_id,
        db=db,
    )
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="콘텐츠 생성에 실패했습니다. 잠시 후 다시 시도해주세요.",
        )

    return ContentGenerateResponse(
        content_type=payload.content_type,
        tone=payload.tone,
        generated_content=content,
        tokens_used=tokens,
        style_profile_id=payload.style_profile_id,
        content_studio_content_id=content_studio_content_id,
    )


# ─── Content Calendar Settings ─────────────────────────────────────────────


@router.post("/calendar", response_model=ContentCalendarSettingRead)
async def create_calendar_setting(
    payload: ContentCalendarSettingCreate,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> ContentCalendarSettingRead:
    setting = await create_content_calendar_setting(db, payload, tenant_id=identity.tenant_id or "")
    return ContentCalendarSettingRead.model_validate(setting)


@router.get("/calendar", response_model=list[ContentCalendarSettingRead])
async def list_calendar_settings(
    account_id: str | None = None,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> list[ContentCalendarSettingRead]:
    settings = await list_content_calendar_settings(db, account_id=account_id, tenant_id=identity.tenant_id)
    return [ContentCalendarSettingRead.model_validate(s) for s in settings]


@router.get("/calendar/{setting_id}", response_model=ContentCalendarSettingRead)
async def get_calendar_setting(
    setting_id: str,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> ContentCalendarSettingRead:
    setting = await get_content_calendar_setting(db, setting_id)
    if setting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="설정을 찾을 수 없습니다.")
    if identity.kind != "admin" and setting.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="다른 테넌트의 설정에 접근할 수 없습니다.")
    return ContentCalendarSettingRead.model_validate(setting)


@router.patch("/calendar/{setting_id}", response_model=ContentCalendarSettingRead)
async def update_calendar_setting(
    setting_id: str,
    payload: ContentCalendarSettingUpdate,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> ContentCalendarSettingRead:
    setting = await update_content_calendar_setting(db, setting_id, payload)
    if setting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="설정을 찾을 수 없습니다.")
    if identity.kind != "admin" and setting.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="다른 테넌트의 설정을 수정할 수 없습니다.")
    return ContentCalendarSettingRead.model_validate(setting)


@router.delete("/calendar/{setting_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_calendar_setting(
    setting_id: str,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> None:
    setting = await get_content_calendar_setting(db, setting_id)
    if setting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="설정을 찾을 수 없습니다.")
    if identity.kind != "admin" and setting.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="다른 테넌트의 설정을 삭제할 수 없습니다.")
    ok = await delete_content_calendar_setting(db, setting_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="설정을 찾을 수 없습니다.")


# ─── Scheduler helper (called from scheduler.py) ───────────────────────────


async def run_daily_content_generation(db: AsyncSession) -> None:
    """Generate daily content for all active content calendar settings.

    For each active setting, generates ``daily_count`` contents of random
    types from ``content_types`` list, creates a broadcast for each, and
    updates ``last_generated_at`` / ``next_generate_at``.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result = await db.execute(
        select(ContentCalendarSetting).where(ContentCalendarSetting.enabled == True)  # noqa: E712
    )
    settings: list[ContentCalendarSetting] = list(result.scalars().all())

    if not settings:
        logger.info("content_calendar_no_active_settings")
        return

    for setting in settings:
        try:
            allowed, reason = await check_ai_quota(db, setting.tenant_id, FEATURE_CONTENT_STUDIO)
            if not allowed:
                logger.warning(
                    "content_calendar_quota_exceeded",
                    setting_id=setting.id,
                    tenant_id=setting.tenant_id,
                    reason=reason,
                )
                continue

            for _ in range(setting.daily_count):
                content_type = random.choice(setting.content_types)
                content, tokens, content_studio_content_id = await generate_content(
                    content_type=content_type,
                    tone=setting.tone,
                    tenant_id=setting.tenant_id,
                )
                if not content:
                    continue

                broadcast_data = {
                    "account_id": setting.account_id,
                    "message": content,
                    "recipients": [],
                    "group_ids": setting.group_ids,
                    "delivery_mode": "normal",
                    "content_studio_content_id": content_studio_content_id,
                }
                broadcast = await broadcast_crud.create_broadcast(
                    db,
                    BroadcastCreate(**broadcast_data),
                    media_path=None,
                    scheduled_at=None,
                )
                logger.info(
                    "content_calendar_broadcast_created",
                    setting_id=setting.id,
                    broadcast_id=broadcast.id,
                    content_type=content_type,
                    tokens=tokens,
                )

            setting.last_generated_at = now
            setting.next_generate_at = now + timedelta(days=1)
            await db.commit()
        except Exception as exc:
            logger.error(
                "content_calendar_generation_failed",
                setting_id=setting.id,
                error=str(exc),
            )
            await db.rollback()
