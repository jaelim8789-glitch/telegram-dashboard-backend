from fastapi import APIRouter, Depends, HTTPException

from app.core.logging import get_logger
from app.api.deps import Identity, get_current_identity
from app.services.ai_growth_coach_service import get_growth_coach_for_identity

router = APIRouter(prefix="/api/ai/growth-coach", tags=["ai-growth-coach"])
logger = get_logger(__name__)


@router.get("/today")
async def get_today_growth_coach(identity: Identity = Depends(get_current_identity)):
    try:
        report = await get_growth_coach_for_identity(identity)
    except ValueError as exc:
        logger.warning("growth_coach_error", error=str(exc))
        raise HTTPException(status_code=403, detail="성장 코치 리포트를 불러올 수 없습니다.")

    return {
        "tenant_id": report.tenant_id,
        "generated_at": report.generated_at,
        "summary": report.summary,
        "todos": report.todos,
        "metrics": report.metrics,
    }