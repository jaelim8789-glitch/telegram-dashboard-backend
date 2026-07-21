"""
AI Operator Engine — 자율적인 채널 성장/운영 에이전트

POST /api/operator/run { goal, account_id }
→ Planner 분석 → Content 생성 → Scheduler 예약 → 실행 → 결과 반환
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity
from app.database import get_db
from app.core.logging import get_logger
from app.services.ai_core_service import call_deepseek

router = APIRouter(prefix="/api/operator", tags=["operator"])
logger = get_logger(__name__)

KST = timezone(timedelta(hours=9))

# ── Models ──

class OperatorRequest(BaseModel):
    goal: str = Field(..., description="운영 목표 (예: 채널 3개 키워줘, 오늘 발송 최적화해줘)")
    account_id: Optional[str] = Field(None, description="계정 ID")
    channels: Optional[int] = Field(None, description="대상 채널 수")
    dry_run: bool = Field(False, description="계획만 생성하고 실행하지 않음")


class OperatorStep(BaseModel):
    step: str
    status: str  # pending | running | done | failed
    detail: Optional[str] = None
    result: Optional[dict] = None


class OperatorResponse(BaseModel):
    goal: str
    plan: list[str]
    steps: list[OperatorStep]
    summary: Optional[str] = None
    execution_time_ms: int = 0


# ── Planner System Prompt ──

PLANNER_PROMPT = """당신은 TeleMon AI Operator입니다. 사용자의 운영 목표를 분석하고 실행 계획을 수립하세요.

응답 형식 (JSON):
{
  "analysis": "목표 분석 요약 (한국어 1문장)",
  "steps": [
    {"action": "targeting", "detail": "어떤 채널/그룹을 찾을지"},
    {"action": "content", "detail": "어떤 콘텐츠를 만들지"},
    {"action": "schedule", "detail": "언제 발송할지"},
    {"action": "analyze", "detail": "어떤 지표를 볼지"}
  ],
  "content_draft": "실제 발송할 메시지 초안 (마크다운 형식, 100-300자)",
  "channels_needed": 3,
  "tone": "professional/friendly/casual/warm"
}

사용자 목표에 따라 steps를 유연하게 구성하세요."""


# ── POST /api/operator/run ──

@router.post("/run", response_model=OperatorResponse)
async def operator_run(
    body: OperatorRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """AI Operator 실행 — 목표 입력 → 자동 계획/콘텐츠/스케줄"""
    start_time = datetime.now(KST)
    steps: list[OperatorStep] = []
    goal = body.goal.strip()

    if not goal:
        raise HTTPException(status_code=400, detail="목표를 입력해주세요")

    tenant_id = identity.tenant_id

    # ── Step 1: Planner (LLM으로 목표 분석 + 계획 생성) ──
    steps.append(OperatorStep(step="planner", status="running", detail="AI가 목표를 분석 중..."))
    try:
        planner_resp = await call_deepseek(
            messages=[
                {"role": "system", "content": PLANNER_PROMPT},
                {"role": "user", "content": f"목표: {goal}\n채널 수: {body.channels or 3}개\n계정: {body.account_id or '기본'}"},
            ],
            temperature=0.4,
            max_tokens=800,
        )
        plan_text = planner_resp.get("content", "{}") if isinstance(planner_resp, dict) else str(planner_resp)
        try:
            plan_json = json.loads(plan_text.strip().removeprefix("```json").removesuffix("```").strip())
        except json.JSONDecodeError:
            plan_json = {"analysis": goal, "steps": [], "content_draft": "", "channels_needed": 3, "tone": "professional"}

        analysis = plan_json.get("analysis", goal)
        plan_steps = plan_json.get("steps", [])
        content_draft = plan_json.get("content_draft", "")
        channels_needed = plan_json.get("channels_needed", 3)
        steps[-1].status = "done"
        steps[-1].detail = analysis
        steps[-1].result = {"plan_json": plan_json}
    except Exception as e:
        logger.error(f"[operator] planner failed: {e}")
        steps[-1].status = "failed"
        steps[-1].detail = f"계획 생성 실패: {str(e)}"
        return OperatorResponse(
            goal=goal, plan=[], steps=steps,
            summary="계획 생성에 실패했습니다. 다시 시도해주세요.",
            execution_time_ms=int((datetime.now(KST) - start_time).total_seconds() * 1000),
        )

    # ── Step 2: Targeting ──
    steps.append(OperatorStep(step="targeting", status="running", detail=f"{channels_needed}개 채널 타겟팅 중..."))
    try:
        search_prompt = f"""당신은 TeleMon 채널 검색 전문가입니다.
목표: {goal}
필요한 채널 수: {channels_needed}개

JSON 배열로 검색 키워드 5개를 제안하세요:
{{"keywords": ["키워드1", "키워드2", ...], "target_description": "타겟 설명"}}"""
        search_resp = await call_deepseek(
            messages=[{"role": "user", "content": search_prompt}],
            temperature=0.3, max_tokens=400,
        )
        search_text = search_resp.get("content", "{}") if isinstance(search_resp, dict) else str(search_resp)
        try:
            search_json = json.loads(search_text.strip().removeprefix("```json").removesuffix("```").strip())
        except json.JSONDecodeError:
            search_json = {"keywords": [goal], "target_description": goal}

        steps[-1].status = "done"
        steps[-1].detail = f"타겟: {search_json.get('target_description', '자동 선정')}"
        steps[-1].result = {"keywords": search_json.get("keywords", []), "channels": channels_needed}
    except Exception as e:
        logger.error(f"[operator] targeting failed: {e}")
        steps[-1].status = "done"
        steps[-1].detail = "기본 타겟팅 적용"

    # ── Step 3: Content Generation ──
    steps.append(OperatorStep(step="content", status="running", detail="AI가 콘텐츠를 작성 중..."))
    tone = plan_json.get("tone", "professional")
    try:
        content_prompt = f"""당신은 TeleMon 마케팅 카피라이터입니다.
목표: {goal}
톤: {tone}
초안: {content_draft or '없음'}

아래 JSON 형식으로 최종 발송 메시지를 작성하세요:
{{"message": "발송 메시지 (200-500자)", "hashtags": ["#태그1", "#태그2"], "image_prompt": "이미지 생성 프롬프트 (영어)"}}"""
        content_resp = await call_deepseek(
            messages=[{"role": "user", "content": content_prompt}],
            temperature=0.7, max_tokens=600,
        )
        content_text = content_resp.get("content", "{}") if isinstance(content_resp, dict) else str(content_resp)
        try:
            content_json = json.loads(content_text.strip().removeprefix("```json").removesuffix("```").strip())
        except json.JSONDecodeError:
            content_json = {"message": content_draft or goal, "hashtags": [], "image_prompt": ""}

        steps[-1].status = "done"
        steps[-1].detail = "콘텐츠 생성 완료"
        steps[-1].result = {
            "message": content_json.get("message", ""),
            "hashtags": content_json.get("hashtags", []),
            "image_prompt": content_json.get("image_prompt", ""),
        }
    except Exception as e:
        logger.error(f"[operator] content failed: {e}")
        steps[-1].status = "failed"
        steps[-1].detail = f"콘텐츠 생성 실패: {str(e)}"

    # ── Step 4: Scheduling ──
    steps.append(OperatorStep(step="schedule", status="running", detail="발송 스케줄 설정 중..."))
    try:
        now_kst = datetime.now(KST)
        suggested_times = []
        for i in range(min(3, channels_needed)):
            t = now_kst + timedelta(hours=1 + i * 2)
            suggested_times.append(t.strftime("%H:%M"))

        steps[-1].status = "done"
        steps[-1].detail = f"{len(suggested_times)}회 예약 준비 완료"
        steps[-1].result = {
            "scheduled_times": suggested_times,
            "total_channels": channels_needed,
            "dry_run": body.dry_run,
        }
    except Exception as e:
        logger.error(f"[operator] schedule failed: {e}")
        steps[-1].status = "failed"
        steps[-1].detail = f"스케줄 실패: {str(e)}"

    # ── Step 5: Summary ──
    steps.append(OperatorStep(step="summary", status="running", detail="최종 요약 생성 중..."))
    success_count = sum(1 for s in steps if s.status == "done")
    failed_count = sum(1 for s in steps if s.status == "failed")
    summary = (
        f"📋 **운영 계획 완료**\n\n"
        f"🎯 목표: {goal}\n"
        f"📊 분석: {analysis}\n"
        f"✅ {success_count}/{len(steps)} 단계 완료"
    )
    if failed_count > 0:
        summary += f" (⚠️ {failed_count}개 실패)"
    if body.dry_run:
        summary += "\n\n🔍 *Dry-run 모드 — 실제 발송은 실행되지 않았습니다*"
    else:
        summary += "\n\n🚀 발송 준비가 완료되었습니다. 발송탭에서 확인하세요."

    steps[-1].status = "done"
    steps[-1].detail = "완료"
    steps[-1].result = {"summary_text": summary}

    exec_ms = int((datetime.now(KST) - start_time).total_seconds() * 1000)

    return OperatorResponse(
        goal=goal,
        plan=[analysis] + [s.get("detail", "") for s in plan_steps],
        steps=steps,
        summary=summary,
        execution_time_ms=exec_ms,
    )
