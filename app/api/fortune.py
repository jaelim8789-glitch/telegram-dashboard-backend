"""Fortune Assistant API    + TeleMon  """

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.deps import get_current_identity, Identity
from app.core.logging import get_logger
from app.services.fortune_service import get_daily_fortune

router = APIRouter(prefix="/api/fortune", tags=["fortune"])
logger = get_logger(__name__)


class FortuneScore(BaseModel):
    relationships: int
    wealth: int
    health: int
    business: int
    luck: int


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
    birth_date: str | None = Query(None, description="Birth date (YYYY-MM-DD)"),
    identity: Identity = Depends(get_current_identity),
):
    """Get daily fortune for the authenticated user."""
    try:
        if birth_date:
            datetime.strptime(birth_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format (YYYY-MM-DD)")

    fortune = await get_daily_fortune(identity.tenant_id or identity.user_id or "unknown", birth_date)
    return fortune


@router.post("/daily", response_model=FortuneResponse)
async def update_birth_and_get_fortune(
    body: FortuneBirthRequest,
    identity: Identity = Depends(get_current_identity),
):
    """Update birth date and get fortune."""
    return await get_daily_fortune(
        identity.tenant_id or identity.user_id or "unknown",
        body.birth_date,
    )
