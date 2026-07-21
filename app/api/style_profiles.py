"""User-facing style profile endpoints — workspace (MyAiTab) access.

Mirrors the admin style-profiles endpoints but authenticated via
user session / API key instead of admin JWT, and scoped to the
authenticated tenant.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Identity, get_current_identity
from app.database import get_db
from app.schemas.style_profile import StyleProfileAnalyzeRequest, StyleProfileRead, StyleProfileUpdate
from app.services.ai_style_service import (
    analyze_style,
    delete_profile,
    get_profile,
    list_profiles,
    update_profile,
)
from app.services.telegram_actions import AccountNotAuthenticatedError

logger = __import__("app.core.logging", fromlist=["get_logger"]).get_logger(__name__)

router = APIRouter(prefix="/api/style-profiles", tags=["style-profiles"])


@router.post("/analyze", response_model=StyleProfileRead, status_code=status.HTTP_201_CREATED)
async def create_style_profile(
    payload: StyleProfileAnalyzeRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """분석할 텍스트를 받아 AI 말투 분석을 수행하고 스타일 프로필을 저장합니다.

    source_type=text: source_text 필드에 직접 텍스트를 붙여넣습니다.
    source_type=channel: account_id + chat_id로 채널을 지정하면 최근 메시지를 자동 수집합니다.
    """
    try:
        profile = await analyze_style(
            name=payload.name,
            source_type=payload.source_type,
            source_text=payload.source_text,
            db=db,
            account_id=payload.account_id,
            chat_id=payload.chat_id,
            message_limit=payload.message_limit,
            tenant_id=identity.tenant_id,
        )
        await db.commit()
        logger.info("style_profile_created", profile_id=profile.id, name=payload.name)
        return profile
    except AccountNotAuthenticatedError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="해당 텔레그램 계정이 인증되지 않았습니다. 계정 설정에서 다시 로그인해주세요.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("", response_model=list[StyleProfileRead])
async def list_style_profiles(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """내 스타일 프로필 목록을 조회합니다. (워크스페이스 드롭다운용)"""
    return await list_profiles(db, tenant_id=identity.tenant_id)


@router.get("/{profile_id}", response_model=StyleProfileRead)
async def get_style_profile(
    profile_id: str,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """특정 스타일 프로필을 조회합니다."""
    profile = await get_profile(db, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="스타일 프로필을 찾을 수 없습니다.")
    if identity.kind != "admin" and profile.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="다른 테넌트의 스타일 프로필에 접근할 수 없습니다.")
    return profile


@router.patch("/{profile_id}", response_model=StyleProfileRead)
async def update_style_profile(
    profile_id: str,
    payload: StyleProfileUpdate,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """스타일 프로필 이름을 수정합니다."""
    profile = await get_profile(db, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="스타일 프로필을 찾을 수 없습니다.")
    if identity.kind != "admin" and profile.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="다른 테넌트의 스타일 프로필을 수정할 수 없습니다.")
    if payload.name is not None:
        profile = await update_profile(db, profile, payload.name)
        await db.commit()
    return profile


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_style_profile(
    profile_id: str,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """스타일 프로필을 삭제합니다."""
    profile = await get_profile(db, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="스타일 프로필을 찾을 수 없습니다.")
    if identity.kind != "admin" and profile.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="다른 테넌트의 스타일 프로필을 삭제할 수 없습니다.")
    await delete_profile(db, profile)
    await db.commit()
