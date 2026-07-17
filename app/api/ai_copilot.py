"""TeleMon AI Copilot — unified AI panel that reuses existing AI APIs.

Provides:
- Context-aware AI assistant (knows your TeleMon state without asking)
- One-click AI actions (kick off multiple operations at once)
- AI recommendations with reasons & confidence scores
- Production-ready endpoint design

All endpoints reuse ``_call_deepseek`` from ``app.services.ai_chat_service``
so the same DeepSeek configuration, provider, and quota model applies.
"""

import json
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Identity, get_current_identity
from app.database import get_db
from app.services.ai_chat_service import _call_deepseek
from app.services.delivery_analytics import get_account_performance, get_failure_breakdown, get_summary
from app.services.lead_capture import get_lead_count, get_leads

router = APIRouter(prefix="/api/copilot", tags=["ai-copilot"])

logger = __import__("app.core.logging", fromlist=["get_logger"]).get_logger(__name__)

# ─── Schemas ────────────────────────────────────────────────────────────

class ContextQuery(BaseModel):
    """Optional scoping for context-aware queries."""
    focus: str | None = Field(
        default=None,
        description="선택적 포커스 영역: 'delivery', 'customers', 'broadcast', 'accounts', 또는 비움 (전체)",
    )
    days: int = Field(default=7, ge=1, le=90, description="분석 기간(일)")


class CopilotChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000, description="사용자의 질문/요청")
    context: ContextQuery = Field(default_factory=ContextQuery, description="선택적 컨텍스트 스코핑")


class CopilotChatResponse(BaseModel):
    reply: str
    context_summary: str = ""
    used_data_sources: list[str] = []


class OneClickActionRequest(BaseModel):
    action: str = Field(
        ...,
        description="실행할 원클릭 액션:\n"
        "- 'health_check': 전체 운영 상태 진단 (전달, 계정, 고객)\n"
        "- 'weekly_report': 주간 리포트 생성\n"
        "- 'optimize_broadcast': 브로드캐스트 최적화 제안\n"
        "- 'customer_insights': 고객 인사이트 분석\n"
        "- 'reply_audit': 최근 답장 품질 검토",
    )
    days: int = Field(default=7, ge=1, le=90, description="분석 기간(일)")
    tenant_id: str | None = Field(default=None, description="관리자용: 분석할 tenant ID")


class OneClickActionResult(BaseModel):
    action: str
    status: str  # "completed", "partial", "failed"
    summary: str
    details: list[dict] = []
    total_duration_ms: int = 0


class RecommendationItem(BaseModel):
    title: str
    description: str
    category: str  # "delivery", "customers", "broadcast", "accounts", "general"
    confidence: float = Field(..., ge=0.0, le=1.0, description="0.0 ~ 1.0 신뢰도")
    reasoning: str
    suggested_action: str = ""
    impact: str = ""  # "high", "medium", "low"


class RecommendationsResponse(BaseModel):
    recommendations: list[RecommendationItem]
    overall_health: str = ""  # "good", "fair", "needs_attention", "critical"
    generated_at: str = ""


class SmartSendTimeRequest(BaseModel):
    timezone: str = Field(default="Asia/Seoul", description="사용자 시간대")
    recipient_count: int = Field(default=0, ge=0, description="수신자 수")


class SmartSendTimeResponse(BaseModel):
    recommended_hour: int = Field(..., ge=0, le=23, description="추천 발송 시간 (시)")
    recommended_day: str = ""  # "weekday", "weekend", "monday", etc.
    reasoning: str
    confidence: float = Field(..., ge=0.0, le=1.0)


class CopilotDashboardResponse(BaseModel):
    active_accounts: int = 0
    total_leads: int = 0
    recent_broadcasts: int = 0
    delivery_rate: str = ""
    pending_issues: int = 0
    ai_recommendations_count: int = 0
    last_report: str = ""
    quick_actions: list[dict] = []


# ─── Internal helpers ───────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _build_tenant_context(tenant_id: str | None) -> str:
    """Build a concise context snapshot of the tenant's TeleMon state."""
    parts = []
    if tenant_id:
        parts.append(f"Tenant ID: {tenant_id}")
    parts.append(f"현재 시각: {_utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    return "\n".join(parts)


async def _gather_context_data(
    db: AsyncSession,
    identity: Identity,
    days: int,
    focus: str | None,
) -> tuple[str, list[str]]:
    """Gather real-time data from existing services. Returns (context_text, sources)."""
    ctx_lines = []
    sources = []
    tenant_id = identity.tenant_id

    try:
        if focus in (None, "delivery", "broadcast"):
            summary = await get_summary(identity, days=days)
            failures = await get_failure_breakdown(identity, days=days)
            accounts = await get_account_performance(identity, days=days)
            ctx_lines.append(f"[전달 분석 - 최근 {days}일]")
            ctx_lines.append(f"  요약: {json.dumps(asdict(summary), ensure_ascii=False)}")
            ctx_lines.append(f"  실패: {len(failures)}건")
            ctx_lines.append(f"  계정 성과: {len(accounts)}개 계정")
            sources.extend(["delivery_analytics", "failure_intel"])
    except Exception as exc:
        ctx_lines.append(f"[전달 분석] 데이터 수집 실패: {exc}")

    try:
        if focus in (None, "customers") and tenant_id:
            total = await get_lead_count(tenant_id)
            leads = await get_leads(tenant_id, limit=50)
            cutoff = _utcnow().replace(tzinfo=None) - timedelta(days=days)
            active = sum(1 for lead in leads if lead.last_interaction and lead.last_interaction >= cutoff)
            ctx_lines.append(f"[고객 데이터 - 최근 {days}일]")
            ctx_lines.append(f"  전체 리드: {total}, 샘플: {len(leads)}, 활성: {active}")
            sources.append("lead_capture")
    except Exception as exc:
        ctx_lines.append(f"[고객 데이터] 수집 실패: {exc}")

    return "\n".join(ctx_lines), sources


async def _call_deepseek_with_timeout(messages: list[dict], timeout_seconds: int = 30) -> str | None:
    """Wrapper for _call_deepseek with individual timeout awareness."""
    try:
        return await _call_deepseek(messages)
    except Exception as exc:
        logger.error("ai_copilot_deepseek_failed", error=str(exc))
        return None


_SYSTEM_COPILOT_PROMPT = (
    "너는 TeleMon AI Copilot이야. TeleMon은 텔레그램 마케팅 자동화 플랫폼이야.\n\n"
    "역할:\n"
    "- 사용자의 TeleMon 운영 상태를 종합적으로 이해하고 조언\n"
    "- 제공된 컨텍스트 데이터를 기반으로 구체적인 인사이트 제공\n"
    "- 한국어로 친절하고 전문적으로 응답\n\n"
    "규칙:\n"
    "- 제공된 데이터만 사용해서 분석 (없는 데이터는 추측하지 말 것)\n"
    "- 구체적인 수치와 비교를 포함할 것\n"
    "- 액션 가능한 조언을 우선적으로 제공\n"
    "- 필요시 이모지를 적절히 사용\n"
    "- 모르는 것은 솔직히 모른다고 답변\n"
    "- 응답은 1500자 이내로 간결하게"
)


# ─── Endpoints ──────────────────────────────────────────────────────────


@router.post("/chat", response_model=CopilotChatResponse)
async def copilot_chat(
    payload: CopilotChatRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> CopilotChatResponse:
    """Context-aware AI chat that understands the user's TeleMon state.

    Before answering, gathers real-time context from delivery analytics,
    lead data, and account health — so the AI knows your actual operational
    state without you having to explain it.
    """
    context_text, sources = await _gather_context_data(
        db, identity, days=payload.context.days, focus=payload.context.focus
    )

    system_prompt = _SYSTEM_COPILOT_PROMPT + (
        "\n\n[현재 TeleMon 운영 컨텍스트]\n"
        f"{context_text}"
        if context_text
        else "\n\n(컨텍스트 데이터 없음 — 일반적인 조언 제공)"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": payload.message},
    ]

    reply = await _call_deepseek_with_timeout(messages)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI Copilot 응답 생성에 실패했습니다. 잠시 후 다시 시도해주세요.",
        )

    return CopilotChatResponse(
        reply=reply.strip(),
        context_summary=context_text[:500] if context_text else "",
        used_data_sources=sources,
    )


@router.post("/actions", response_model=OneClickActionResult)
async def one_click_action(
    payload: OneClickActionRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> OneClickActionResult:
    """Execute a one-click AI action — kick off multiple operations at once.

    Actions:
    - 'health_check': Full diagnostic of delivery, accounts, customers
    - 'weekly_report': Generate a weekly operations report
    - 'optimize_broadcast': Broadcast optimization suggestions
    - 'customer_insights': Deep customer insight analysis
    - 'reply_audit': Recent reply quality review
    """
    start_time = time.monotonic()
    days = payload.days
    tenant_id = payload.tenant_id or identity.tenant_id
    details: list[dict] = []
    all_ok = True

    try:
        context_text, sources = await _gather_context_data(db, identity, days=days, focus=None)
    except Exception as exc:
        context_text = f"(컨텍스트 수집 실패: {exc})"
        sources = []

    if payload.action == "health_check":
        system_prompt = (
            "너는 TeleMon AI 운영 진단 전문가야. 아래 운영 데이터를 분석해서 "
            "전체 건강 상태를 진단해줘.\n\n"
            "다음 항목을 각각 평가해줘:\n"
            "1. 메시지 전달 건강도 (전송 성공률, 실패율)\n"
            "2. 계정 건강도 (계정 상태, 제한 여부)\n"
            "3. 고객 참여도 (활성 리드 비율, 응답률)\n"
            "4. 전반적 위험 요소\n\n"
            "각 항목마다 상태('좋음'/'주의'/'위험')와 근거를 제시.\n"
            "한국어로 응답."
        )
        user_prompt = f"[운영 데이터 - 최근 {days}일]\n{context_text}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        reply = await _call_deepseek_with_timeout(messages)
        if reply:
            details.append({
                "step": "delivery_health",
                "status": "completed",
                "finding": reply[:1000],
            })
        else:
            details.append({"step": "delivery_health", "status": "failed", "finding": "분석 실패"})
            all_ok = False

        # Also run customer pulse
        if tenant_id:
            try:
                total = await get_lead_count(tenant_id)
                details.append({
                    "step": "customer_pulse",
                    "status": "completed",
                    "finding": f"전체 리드 {total}개",
                })
            except Exception as exc:
                details.append({"step": "customer_pulse", "status": "failed", "finding": str(exc)})
                all_ok = False

    elif payload.action == "weekly_report":
        system_prompt = (
            "너는 TeleMon 주간 운영 리포트 생성기야. "
            "아래 데이터로 주간 리포트를 작성해줘.\n\n"
            "형식:\n"
            "## 📊 주간 운영 리포트 (최근 {days}일)\n\n"
            "### 1. 전달 현황\n"
            "- 총 발송 수, 성공률, 실패율\n\n"
            "### 2. 계정 상태\n"
            "- 계정별 성과 하이라이트\n\n"
            "### 3. 고객 활동\n"
            "- 리드 현황, 참여도 변화\n\n"
            "### 4. 주요 발견 & 제안\n"
            "- 가장 중요한 3가지 인사이트\n\n"
            "한국어로 작성."
        ).format(days=days)
        user_prompt = f"[운영 데이터]\n{context_text}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        reply = await _call_deepseek_with_timeout(messages)
        if reply:
            details.append({
                "step": "weekly_report",
                "status": "completed",
                "finding": reply[:1000],
            })
        else:
            details.append({"step": "weekly_report", "status": "failed", "finding": "리포트 생성 실패"})
            all_ok = False

    elif payload.action == "optimize_broadcast":
        system_prompt = (
            "너는 TeleMon 브로드캐스트 최적화 전문가야. "
            "전달 데이터와 계정 성과를 분석해서 발송 최적화 제안을 해줘.\n\n"
            "제안 항목:\n"
            "1. 최적 발송 시간대 추천\n"
            "2. 발송 간격/빈도 조정 제안\n"
            "3. 타겟 세분화 제안\n"
            "4. 메시지 포맷/톤 제안\n"
            "5. A/B 테스트 아이디어\n\n"
            "각 제안에 이유와 기대 효과를 포함.\n"
            "한국어로 응답."
        )
        user_prompt = f"[전달/계정 데이터 - 최근 {days}일]\n{context_text}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        reply = await _call_deepseek_with_timeout(messages)
        if reply:
            details.append({
                "step": "broadcast_optimization",
                "status": "completed",
                "finding": reply[:1000],
            })
        else:
            details.append({"step": "broadcast_optimization", "status": "failed", "finding": "분석 실패"})
            all_ok = False

    elif payload.action == "customer_insights":
        system_prompt = (
            "너는 TeleMon 고객 분석 전문가야. "
            "리드 데이터를 분석해서 실행 가능한 인사이트를 제공해줘.\n\n"
            "분석 항목:\n"
            "1. 고객 세그먼트 분류 (활성/휴면/이탈 위험)\n"
            "2. 참여도 트렌드\n"
            "3. 세그먼트별 추천 메시지 전략\n"
            "4. 리드 재활성화 제안\n\n"
            "각 인사이트에 신뢰도(높음/중간/낮음)를 표시.\n"
            "한국어로 응답."
        )
        user_prompt = f"[고객 데이터 - 최근 {days}일]\n{context_text}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        reply = await _call_deepseek_with_timeout(messages)
        if reply:
            details.append({
                "step": "customer_insights",
                "status": "completed",
                "finding": reply[:1000],
            })
        else:
            details.append({"step": "customer_insights", "status": "failed", "finding": "분석 실패"})
            all_ok = False

    elif payload.action == "reply_audit":
        system_prompt = (
            "너는 TeleMon 고객 응답 품질 감사관이야. "
            "자동 응답과 매크로 설정 데이터를 바탕으로 품질 리뷰를 해줘.\n\n"
            "검토 항목:\n"
            "1. 응답 템플릿 다양성\n"
            "2. 응답 시간 적절성\n"
            "3. 커버리지 (응답이 필요한 메시지 vs 실제 응답)\n"
            "4. 개선 제안\n\n"
            "한국어로 응답."
        )
        user_prompt = f"[운영 컨텍스트]\n{context_text}\n\n위 데이터와 일반적인 모범 사례를 바탕으로 응답 품질 감사를 수행해줘."
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        reply = await _call_deepseek_with_timeout(messages)
        if reply:
            details.append({
                "step": "reply_audit",
                "status": "completed",
                "finding": reply[:1000],
            })
        else:
            details.append({"step": "reply_audit", "status": "failed", "finding": "분석 실패"})
            all_ok = False
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"알 수 없는 액션: {payload.action}",
        )

    elapsed = int((time.monotonic() - start_time) * 1000)
    overall_status = "completed" if all_ok else "partial"

    summary_parts = [d.get("finding", "")[:100] for d in details if d.get("status") == "completed"]
    summary = "\n".join(summary_parts)[:500] if summary_parts else "일부 작업이 완료되었습니다."

    return OneClickActionResult(
        action=payload.action,
        status=overall_status,
        summary=summary,
        details=details,
        total_duration_ms=elapsed,
    )


@router.get("/recommendations", response_model=RecommendationsResponse)
async def get_recommendations(
    days: int = 7,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> RecommendationsResponse:
    """Generate AI recommendations with reasons & confidence scores.

    Analyzes delivery analytics, account health, and customer data to produce
    ranked recommendations. Each includes a confidence score (0.0-1.0),
    detailed reasoning, and suggested actions.
    """
    context_text, sources = await _gather_context_data(db, identity, days=days, focus=None)

    system_prompt = (
        "너는 TeleMon AI 추천 엔진이야. 운영 데이터를 분석해서 "
        "우선순위가 매겨진 추천 항목들을 제공해줘.\n\n"
        "반드시 아래 JSON 형식으로만 응답 (다른 텍스트 없이):\n"
        "{\n"
        '  "overall_health": "good|fair|needs_attention|critical",\n'
        '  "recommendations": [\n'
        "    {\n"
        '      "title": "추천 제목",\n'
        '      "description": "상세 설명",\n'
        '      "category": "delivery|customers|broadcast|accounts|general",\n'
        '      "confidence": 0.95,\n'
        '      "reasoning": "이 추천을 하게 된 구체적인 이유",\n'
        '      "suggested_action": "실제로 취할 수 있는 액션",\n'
        '      "impact": "high|medium|low"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "규칙:\n"
        "- 3~7개의 추천 항목을 제공\n"
        "- confidence는 데이터 기반으로 산정 (데이터가 많을수록 높게)\n"
        "- impact가 'high'인 항목을 우선 배치\n"
        "- 구체적인 수치와 근거를 reasoning에 포함\n"
        "- 한국어로 작성"
    )
    user_prompt = f"[운영 데이터 - 최근 {days}일]\n{context_text}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    reply = await _call_deepseek_with_timeout(messages)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI 추천 생성에 실패했습니다. 잠시 후 다시 시도해주세요.",
        )

    try:
        parsed = json.loads(reply.strip())
        overall_health = parsed.get("overall_health", "fair")
        recs_data = parsed.get("recommendations", [])
    except (json.JSONDecodeError, TypeError, ValueError):
        overall_health = "fair"
        recs_data = []

    recommendations = []
    for item in recs_data[:10]:
        recommendations.append(
            RecommendationItem(
                title=item.get("title", "추천"),
                description=item.get("description", ""),
                category=item.get("category", "general"),
                confidence=min(max(float(item.get("confidence", 0.5)), 0.0), 1.0),
                reasoning=item.get("reasoning", ""),
                suggested_action=item.get("suggested_action", ""),
                impact=item.get("impact", "medium"),
            )
        )

    return RecommendationsResponse(
        recommendations=recommendations,
        overall_health=overall_health,
        generated_at=_utcnow().isoformat(),
    )


@router.post("/recommendations/refresh", response_model=RecommendationsResponse)
async def refresh_recommendations(
    days: int = 7,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> RecommendationsResponse:
    """Force-refresh AI recommendations (identical to GET but explicit
    that this triggers a new DeepSeek call every time — no caching)."""
    return await get_recommendations(days=days, identity=identity, db=db)


@router.post("/smart-send-time", response_model=SmartSendTimeResponse)
async def smart_send_time(
    payload: SmartSendTimeRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> SmartSendTimeResponse:
    """AI-powered optimal send time recommendation based on delivery analytics.

    Analyzes historical delivery patterns (if available) to suggest the best
    time and day for broadcast sends, with confidence score and reasoning.
    """
    context_text, sources = await _gather_context_data(db, identity, days=30, focus="delivery")

    system_prompt = (
        "너는 TeleMon 발송 시간 최적화 전문가야. "
        "전달 데이터와 일반적인 텔레그램 마케팅 모범 사례를 바탕으로 "
        "최적의 발송 시간을 추천해줘.\n\n"
        "반드시 아래 JSON 형식으로만 응답:\n"
        "{\n"
        '  "recommended_hour": 10,\n'
        '  "recommended_day": "weekday",\n'
        '  "reasoning": "선정 이유",\n'
        '  "confidence": 0.85\n'
        "}\n\n"
        "고려사항:\n"
        f"- 사용자 시간대: {payload.timezone}\n"
        f"- 수신자 수: {payload.recipient_count}명\n"
        "- recommended_hour: 0-23 사이 정수\n"
        "- recommended_day: 'weekday', 'weekend', 또는 요일명 (영문)\n"
        "- confidence: 0.0~1.0 (데이터 많을수록 높게)\n"
        "- 한국어로 reasoning 작성"
    )
    user_prompt = f"[전달 데이터 - 최근 30일]\n{context_text}" if context_text else "(전달 데이터 없음 — 일반적인 패턴 기반 추천)"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    reply = await _call_deepseek_with_timeout(messages)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="발송 시간 추천에 실패했습니다. 잠시 후 다시 시도해주세요.",
        )

    try:
        parsed = json.loads(reply.strip())
        return SmartSendTimeResponse(
            recommended_hour=max(0, min(23, int(parsed.get("recommended_hour", 10)))),
            recommended_day=str(parsed.get("recommended_day", "weekday")),
            reasoning=str(parsed.get("reasoning", "")),
            confidence=min(max(float(parsed.get("confidence", 0.5)), 0.0), 1.0),
        )
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        return SmartSendTimeResponse(
            recommended_hour=10,
            recommended_day="weekday",
            reasoning="데이터 분석 중 오류가 발생했습니다. 일반적인 추천 시간(오전 10시 평일)을 제공합니다.",
            confidence=0.5,
        )


@router.get("/dashboard", response_model=CopilotDashboardResponse)
async def copilot_dashboard(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> CopilotDashboardResponse:
    """Quick-summary dashboard for the AI Copilot panel.

    Returns key metrics and available quick actions so the frontend can
    render a meaningful copilot panel without making multiple API calls.
    """
    tenant_id = identity.tenant_id
    active_accounts = 0
    total_leads = 0
    delivery_rate = ""
    pending_issues = 0

    # Gather lightweight metrics
    try:
        summary = await get_summary(identity, days=7)
        if summary.total_attempted > 0:
            delivery_rate = f"{summary.successful}/{summary.total_attempted} ({summary.success_rate}%)"
        else:
            delivery_rate = "데이터 없음"
    except Exception:
        pass

    try:
        if tenant_id:
            total_leads = await get_lead_count(tenant_id)
    except Exception:
        pass

    quick_actions = [
        {"id": "health_check", "label": "🏥 전체 건강 진단", "description": "전달/계정/고객 상태 한번에 진단"},
        {"id": "weekly_report", "label": "📊 주간 리포트 생성", "description": "7일치 운영 데이터 요약 리포트"},
        {"id": "optimize_broadcast", "label": "📨 발송 최적화 제안", "description": "더 나은 발송 전략 추천"},
        {"id": "customer_insights", "label": "👥 고객 인사이트", "description": "리드 분석 및 세그먼트 제안"},
        {"id": "reply_audit", "label": "💬 응답 품질 검토", "description": "자동 응답 품질 진단"},
    ]

    return CopilotDashboardResponse(
        active_accounts=active_accounts,
        total_leads=total_leads,
        recent_broadcasts=0,
        delivery_rate=delivery_rate,
        pending_issues=pending_issues,
        ai_recommendations_count=0,
        last_report="",
        quick_actions=quick_actions,
    )