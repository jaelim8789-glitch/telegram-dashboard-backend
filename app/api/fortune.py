"""Fortune Assistant API — 오늘의 운세 + TeleMon 최적 시간"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.deps import get_current_identity, Identity
from app.core.logging import get_logger
from app.services.fortune_service import get_daily_fortune

router = APIRouter(prefix="/api/fortune", tags=["fortune"])
logger = get_logger(__name__)


class FortuneScore(BaseModel):
    사업운: int
    재물운: int
    대인운: int
    건강운: int
    커뮤니케이션운: int


class BroadcastAdvice(BaseModel):
    broadcast_best_time: str
    group_engage_time: str
    reply_peak_time: str


class WeeklyOutlook(BaseModel):
    trend: str
    focus: str
    risk: str


class MonthlyFlow(BaseModel):
    overall_mood: str
    peak_week: int
    opportunity: str


class FortuneResponse(BaseModel):
    date: str
    zodiac: str
    grade: str
    summary: str
    scores: FortuneScore
    overall_score: int
    advice: BroadcastAdvice
    lucky_keywords: list[str]
    avoid_today: list[str]
    core_missions: list[str]
    weekly: WeeklyOutlook
    monthly: MonthlyFlow
    lucky_numbers: list[int]
    lucky_colors: list[str]
    generated_at: str


class FortuneBirthRequest(BaseModel):
    birth_date: str | None = None


@router.get("/daily", response_model=FortuneResponse)
async def get_daily_fortune_endpoint(
    birth_date: str | None = Query(None, description="생년월일 (YYYY-MM-DD)"),
    identity: Identity = Depends(get_current_identity),
):
    """🍀 오늘의 운세를 TeleMon 운영 최적 시간과 함께 제공합니다."""
    try:
        if birth_date:
            datetime.strptime(birth_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="날짜 형식이 올바르지 않습니다 (YYYY-MM-DD)")
    
    fortune = await get_daily_fortune(identity.tenant_id or identity.user_id or "unknown", birth_date)
    return fortune


@router.post("/daily", response_model=FortuneResponse)
async def update_birth_and_get_fortune(
    body: FortuneBirthRequest,
    identity: Identity = Depends(get_current_identity),
):
    """생년월일을 저장하고 오늘의 운세를 반환합니다."""
    return await get_daily_fortune(
        identity.tenant_id or identity.user_id or "unknown",
        body.birth_date,
    )
