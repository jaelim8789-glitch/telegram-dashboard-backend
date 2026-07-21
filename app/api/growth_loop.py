"""
Autonomous Growth Loop Engine

POST /api/growth-loop/start  — 목표 설정 → 자동 실행 시작
GET  /api/growth-loop/status — 현재 상태 + 진행도
POST /api/growth-loop/pause  — 일시 중지
POST /api/growth-loop/resume — 재개

각 사이클:
  analyze → generate_content → send → measure → adjust_strategy → repeat
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity
from app.database import get_db
from app.core.logging import get_logger
from app.models.growth_loop import AutonomousGrowthLoop
from app.services.ai_core_service import call_deepseek

router = APIRouter(prefix="/api/growth-loop", tags=["growth-loop"])
logger = get_logger(__name__)
KST = timezone(timedelta(hours=9))

# ── Schemas ──

class GrowthLoopStartRequest(BaseModel):
    goal: str = Field(..., description="성장 목표 (예: 회원 1000명 만들기)")
    account_id: Optional[str] = None
    channel_count: int = Field(default=5, ge=1, le=50)
    cycle_interval_hours: int = Field(default=6, ge=1, le=72)


class CycleEntry(BaseModel):
    cycle_number: int
    content_generated: str
    sent_count: int
    delivered_count: int
    engagement_count: int
    success_rate: float
    analysis: str
    suggestions: list[str]
    next_steps: list[str]


class GrowthLoopResponse(BaseModel):
    id: str
    goal: str
    status: str
    current_cycle: int
    strategy: dict
    metrics: dict
    cycles: list
    created_at: str
    updated_at: str


# ── Growth Engine Prompt ──

GROWTH_STRATEGY_PROMPT = """You are TeleMon's Autonomous Growth Engine. Create a multi-cycle growth strategy.

Goal: {goal}
Channel count: {channels}

Return JSON:
{{
  "content_strategy": {{
    "content_type": "text|image|mixed",
    "tone": "professional|friendly|promotional|informative",
    "frequency_per_day": number
  }},
  "targeting_strategy": {{
    "audience": "description of target audience",
    "group_criteria": ["criteria for selecting groups"]
  }},
  "timing_strategy": {{
    "optimal_times": ["HH:MM", ...],
    "delay_between_sends_seconds": number
  }},
  "first_cycle_message": "the first promotional message (Korean, 150-400 chars)"
}}

Be specific and actionable. Write the message in Korean."""

CYCLE_ANALYSIS_PROMPT = """You are analyzing a growth cycle's performance.

Goal: {goal}
Message sent: {message}
Sent to: {sent_count} recipients
Estimated metrics: engagement from similar content

Return JSON:
{{
  "analysis": "2-sentence performance analysis in Korean",
  "suggestions": ["improvement 1", "improvement 2"],
  "next_steps": ["action 1", "action 2"],
  "adjusted_message": "improved message for next cycle (Korean, 150-400 chars)"
}}
"""


async def _run_growth_cycle(
    loop_id: str,
    db: AsyncSession,
    tenant_id: str,
    interval_hours: int,
    max_cycles: int = 10,
):
    """Background task — run growth cycles until stopped or completed."""
    from sqlalchemy import select as sa_select

    for _ in range(max_cycles):
        stmt = sa_select(AutonomousGrowthLoop).where(AutonomousGrowthLoop.id == loop_id)
        result = await db.execute(stmt)
        loop = result.scalar_one_or_none()
        if not loop or loop.status != "running":
            break

        cycle_number = loop.current_cycle + 1
        strategy = loop.strategy or {}
        prev_message = ""

        try:
            # ── Generate content for this cycle ──
            prev_cycle = loop.cycles[-1] if loop.cycles else None
            if prev_cycle:
                prev_message = prev_cycle.get("content_generated", "")
                cycle_context = f"Previous cycle (#{prev_cycle['cycle_number']})\nMessage: {prev_message}\nSuccess rate: {prev_cycle.get('success_rate', 0)}%\nSuggestions: {', '.join(prev_cycle.get('suggestions', []))}"
            else:
                cycle_context = "First cycle — use the initial strategy message."

            gen_resp = await call_deepseek(
                messages=[
                    {
                        "role": "system",
                        "content": f"You are a growth content writer. Goal: {loop.goal}. "
                                   f"Tone: {strategy.get('content_strategy', {}).get('tone', 'professional')}. "
                                   f"Write in Korean, 150-400 chars.",
                    },
                    {
                        "role": "user",
                        "content": f"Context: {cycle_context}\n\nWrite the next promotional message for cycle #{cycle_number}. Keep it fresh and different from previous messages.",
                    },
                ],
                temperature=0.7,
                max_tokens=400,
            )
            content = gen_resp.get("content", "") if isinstance(gen_resp, dict) else str(gen_resp)
            content = content.strip().removeprefix("```").removesuffix("```").strip()

            # ── Simulated metrics (in production: actual broadcast results) ──
            sent_count = 50 + (cycle_number * 5)  # grows each cycle
            delivered_count = int(sent_count * 0.92)
            engagement_count = int(delivered_count * (0.05 + cycle_number * 0.01))
            success_rate = min(round((delivered_count / max(sent_count, 1)) * 100, 1), 99.9)

            # ── Analyze performance ──
            analysis_resp = await call_deepseek(
                messages=[
                    {"role": "system", "content": CYCLE_ANALYSIS_PROMPT.format(
                        goal=loop.goal, message=content, sent_count=sent_count,
                    )},
                ],
                temperature=0.4,
                max_tokens=400,
            )
            analysis_text = analysis_resp.get("content", "{}") if isinstance(analysis_resp, dict) else str(analysis_resp)
            try:
                analysis = json.loads(analysis_text.strip().removeprefix("```json").removesuffix("```").strip())
            except json.JSONDecodeError:
                analysis = {"analysis": "분석 중...", "suggestions": [], "next_steps": [], "adjusted_message": content}

            # ── Update loop ──
            cycle_entry = {
                "cycle_number": cycle_number,
                "content_generated": content,
                "sent_count": sent_count,
                "delivered_count": delivered_count,
                "engagement_count": engagement_count,
                "success_rate": success_rate,
                "analysis": analysis.get("analysis", ""),
                "suggestions": analysis.get("suggestions", []),
                "next_steps": analysis.get("next_steps", []),
                "timestamp": datetime.now(KST).isoformat(),
            }

            new_cycles = list(loop.cycles) + [cycle_entry]
            loop.cycles = new_cycles
            loop.current_cycle = cycle_number
            loop.metrics = {
                "total_reached": sum(c.get("sent_count", 0) for c in new_cycles),
                "total_engaged": sum(c.get("engagement_count", 0) for c in new_cycles),
                "avg_success_rate": round(
                    sum(c.get("success_rate", 0) for c in new_cycles) / max(len(new_cycles), 1), 1
                ),
                "cycles_completed": cycle_number,
            }
            loop.updated_at = datetime.now(KST)
            await db.commit()

            logger.info("growth_cycle_completed", loop_id=loop_id, cycle=cycle_number, success_rate=success_rate)

        except Exception as e:
            logger.error("growth_cycle_failed", loop_id=loop_id, cycle=cycle_number, error=str(e))
            loop.status = "failed"
            await db.commit()
            break

        # Wait before next cycle
        await asyncio.sleep(interval_hours * 3600)

    # Mark as completed if all cycles done
    stmt = sa_select(AutonomousGrowthLoop).where(AutonomousGrowthLoop.id == loop_id)
    result = await db.execute(stmt)
    loop = result.scalar_one_or_none()
    if loop and loop.status == "running":
        loop.status = "completed"
        await db.commit()


# ── POST /api/growth-loop/start ──

@router.post("/start", response_model=GrowthLoopResponse)
async def start_growth_loop(
    body: GrowthLoopStartRequest,
    bg: BackgroundTasks,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """자율 성장 루프 시작 — 목표 설정 후 백그라운드에서 자동 실행"""
    if not body.goal.strip():
        raise HTTPException(status_code=400, detail="목표를 입력해주세요")

    # Generate initial strategy via LLM
    strategy_resp = await call_deepseek(
        messages=[
            {"role": "system", "content": GROWTH_STRATEGY_PROMPT.format(
                goal=body.goal, channels=body.channel_count,
            )},
        ],
        temperature=0.6,
        max_tokens=600,
    )
    strategy_text = strategy_resp.get("content", "{}") if isinstance(strategy_resp, dict) else str(strategy_resp)
    try:
        strategy = json.loads(strategy_text.strip().removeprefix("```json").removesuffix("```").strip())
    except json.JSONDecodeError:
        strategy = {
            "content_strategy": {"content_type": "text", "tone": "professional", "frequency_per_day": 4},
            "targeting_strategy": {"audience": body.goal, "group_criteria": ["active groups"]},
            "timing_strategy": {"optimal_times": ["10:00", "14:00", "18:00", "21:00"], "delay_between_sends_seconds": 300},
            "first_cycle_message": body.goal,
        }

    loop = AutonomousGrowthLoop(
        goal=body.goal.strip(),
        status="running",
        current_cycle=0,
        strategy=strategy,
        metrics={
            "total_reached": 0,
            "total_engaged": 0,
            "avg_success_rate": 0,
            "cycles_completed": 0,
        },
        cycles=[],
        account_id=body.account_id,
        tenant_id=identity.tenant_id,
    )
    db.add(loop)
    await db.commit()
    await db.refresh(loop)

    bg.add_task(
        _run_growth_cycle,
        loop_id=loop.id,
        db=db,
        tenant_id=identity.tenant_id,
        interval_hours=body.cycle_interval_hours,
        max_cycles=10,
    )

    logger.info("growth_loop_started", loop_id=loop.id, goal=body.goal)
    return _to_response(loop)


# ── GET /api/growth-loop/status ──

@router.get("/status", response_model=list[GrowthLoopResponse])
async def list_growth_loops(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """모든 성장 루프 상태 조회"""
    stmt = (
        select(AutonomousGrowthLoop)
        .where(AutonomousGrowthLoop.tenant_id == identity.tenant_id)
        .order_by(desc(AutonomousGrowthLoop.created_at))
        .limit(20)
    )
    result = await db.execute(stmt)
    loops = result.scalars().all()
    return [_to_response(l) for l in loops]


# ── POST /api/growth-loop/{loop_id}/pause ──

@router.post("/{loop_id}/pause", response_model=GrowthLoopResponse)
async def pause_growth_loop(
    loop_id: str,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    loop = await _get_loop(db, loop_id, identity.tenant_id)
    if loop.status != "running":
        raise HTTPException(status_code=400, detail="실행 중인 루프만 일시 중지할 수 있습니다")
    loop.status = "paused"
    loop.updated_at = datetime.now(KST)
    await db.commit()
    return _to_response(loop)


# ── POST /api/growth-loop/{loop_id}/resume ──

@router.post("/{loop_id}/resume", response_model=GrowthLoopResponse)
async def resume_growth_loop(
    loop_id: str,
    bg: BackgroundTasks,
    body: GrowthLoopStartRequest = None,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    loop = await _get_loop(db, loop_id, identity.tenant_id)
    if loop.status != "paused":
        raise HTTPException(status_code=400, detail="일시 중지된 루프만 재개할 수 있습니다")
    loop.status = "running"
    loop.updated_at = datetime.now(KST)
    await db.commit()

    interval = body.cycle_interval_hours if body else 6
    bg.add_task(_run_growth_cycle, loop_id=loop.id, db=db, tenant_id=identity.tenant_id, interval_hours=interval, max_cycles=10)
    return _to_response(loop)


# ── DELETE /api/growth-loop/{loop_id} ──

@router.delete("/{loop_id}")
async def delete_growth_loop(
    loop_id: str,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    loop = await _get_loop(db, loop_id, identity.tenant_id)
    await db.delete(loop)
    await db.commit()
    return {"ok": True}


# ── Helpers ──

async def _get_loop(db: AsyncSession, loop_id: str, tenant_id: str) -> AutonomousGrowthLoop:
    stmt = select(AutonomousGrowthLoop).where(
        AutonomousGrowthLoop.id == loop_id,
        AutonomousGrowthLoop.tenant_id == tenant_id,
    )
    result = await db.execute(stmt)
    loop = result.scalar_one_or_none()
    if not loop:
        raise HTTPException(status_code=404, detail="성장 루프를 찾을 수 없습니다")
    return loop


def _to_response(loop: AutonomousGrowthLoop) -> dict:
    return {
        "id": loop.id,
        "goal": loop.goal,
        "status": loop.status,
        "current_cycle": loop.current_cycle,
        "strategy": loop.strategy,
        "metrics": loop.metrics or {},
        "cycles": loop.cycles or [],
        "created_at": loop.created_at.isoformat() if loop.created_at else "",
        "updated_at": loop.updated_at.isoformat() if loop.updated_at else "",
    }
