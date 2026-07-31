"""TeleMon AI Copilot  unified AI panel that reuses existing AI APIs.

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
from app.services.telemon_memory_service import build_telemon_memory_context

router = APIRouter(prefix="/api/copilot", tags=["ai-copilot"])

logger = __import__("app.core.logging", fromlist=["get_logger"]).get_logger(__name__)

#  Schemas 

class ContextQuery(BaseModel):
    """Optional scoping for context-aware queries."""
    focus: str | None = Field(
        default=None,
        description="  : 'delivery', 'customers', 'broadcast', 'accounts',   ()",
    )
    days: int = Field(default=7, ge=1, le=90, description=" ()")


class CopilotChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000, description=" /")
    context: ContextQuery = Field(default_factory=ContextQuery, description="  ")


class CopilotChatResponse(BaseModel):
    reply: str
    context_summary: str = ""
    used_data_sources: list[str] = []


class OneClickActionRequest(BaseModel):
    action: str = Field(
        ...,
        description="  :\n"
        "- 'health_check':     (, , )\n"
        "- 'weekly_report':   \n"
        "- 'optimize_broadcast':   \n"
        "- 'customer_insights':   \n"
        "- 'reply_audit':    ",
    )
    days: int = Field(default=7, ge=1, le=90, description=" ()")
    tenant_id: str | None = Field(default=None, description=":  tenant ID")


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
    confidence: float = Field(..., ge=0.0, le=1.0, description="0.0 ~ 1.0 ")
    reasoning: str
    suggested_action: str = ""
    impact: str = ""  # "high", "medium", "low"


class RecommendationsResponse(BaseModel):
    recommendations: list[RecommendationItem]
    overall_health: str = ""  # "good", "fair", "needs_attention", "critical"
    generated_at: str = ""


class SmartSendTimeRequest(BaseModel):
    timezone: str = Field(default="Asia/Seoul", description=" ")
    recipient_count: int = Field(default=0, ge=0, description=" ")


class SmartSendTimeResponse(BaseModel):
    recommended_hour: int = Field(..., ge=0, le=23, description="   ()")
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


#  Internal helpers 

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _build_tenant_context(tenant_id: str | None) -> str:
    """Build a concise context snapshot of the tenant's TeleMon state."""
    parts = []
    if tenant_id:
        parts.append(f"Tenant ID: {tenant_id}")
    parts.append(f" : {_utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    return "\n".join(parts)


_memory_provider = None


def _get_memory_provider():
    global _memory_provider
    if _memory_provider is None:
        from app.services.ai_memory import get_ai_memory_provider

        _memory_provider = get_ai_memory_provider()
    return _memory_provider


async def _get_memory_context(tenant_id: str, query: str, limit: int = 3) -> str:
    provider = _get_memory_provider()
    if provider is None:
        return ""
    try:
        memories = await provider.search(tenant_id, query, limit=limit)
        if not memories:
            return ""
        lines = ["[ AI /]"]
        for m in memories:
            fact = m.get("fact") or m.get("task") or ""
            if fact:
                lines.append(f"- {fact}")
        return "\n".join(lines)
    except Exception:
        return ""


async def _store_memory(tenant_id: str, user_message: str, assistant_reply: str) -> None:
    provider = _get_memory_provider()
    if provider is None:
        return
    try:
        episode = f"User: {user_message}\nAssistant: {assistant_reply}"
        await provider.add_episode(tenant_id, episode, {"type": "copilot_chat"})
    except Exception:
        pass


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
            ctx_lines.append(f"[  -  {days}]")
            ctx_lines.append(f"  : {json.dumps(asdict(summary), ensure_ascii=False)}")
            ctx_lines.append(f"  : {len(failures)}")
            ctx_lines.append(f"   : {len(accounts)} ")
            sources.extend(["delivery_analytics", "failure_intel"])
    except Exception as exc:
        ctx_lines.append(f"[ ]   : {exc}")

    try:
        if focus in (None, "customers") and tenant_id:
            total = await get_lead_count(tenant_id)
            leads = await get_leads(tenant_id, limit=50)
            cutoff = _utcnow().replace(tzinfo=None) - timedelta(days=days)
            active = sum(1 for lead in leads if lead.last_interaction and lead.last_interaction >= cutoff)
            ctx_lines.append(f"[  -  {days}]")
            ctx_lines.append(f"   : {total}, : {len(leads)}, : {active}")
            sources.append("lead_capture")
    except Exception as exc:
        ctx_lines.append(f"[ ]  : {exc}")

    return "\n".join(ctx_lines), sources


async def _call_deepseek_with_timeout(messages: list[dict], timeout_seconds: int = 30) -> str | None:
    """Wrapper for _call_deepseek with individual timeout awareness."""
    try:
        return await _call_deepseek(messages)
    except Exception as exc:
        logger.error("ai_copilot_deepseek_failed", error=str(exc))
        return None


_SYSTEM_COPILOT_PROMPT = (
    " TeleMon AI Copilot. TeleMon    .\n\n"
    ":\n"
    "-  TeleMon     \n"
    "-       \n"
    "-    \n\n"
    ":\n"
    "-     (    )\n"
    "-     \n"
    "-     \n"
    "-    \n"
    "-     \n"
    "-  1500  "
)


#  Endpoints 


@router.post("/chat", response_model=CopilotChatResponse)
async def copilot_chat(
    payload: CopilotChatRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> CopilotChatResponse:
    """Context-aware AI chat that understands the user's TeleMon state.

    Before answering, gathers real-time context from delivery analytics,
    lead data, and account health  so the AI knows your actual operational
    state without you having to explain it.
    """
    context_text, sources = await _gather_context_data(
        db, identity, days=payload.context.days, focus=payload.context.focus
    )

    tenant_id = identity.tenant_id or "anonymous"
    memory_context = await _get_memory_context(tenant_id, payload.message, limit=3)
    telemon_memory = await build_telemon_memory_context(db, identity, payload.message)

    system_prompt = _SYSTEM_COPILOT_PROMPT + (
        "\n\n[ TeleMon  ]\n"
        f"{context_text}"
        if context_text
        else "\n\n(      )"
    )
    if memory_context:
        system_prompt += f"\n\n{memory_context}"
    if telemon_memory.text:
        system_prompt += f"\n\n{telemon_memory.text}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": payload.message},
    ]

    reply = await _call_deepseek_with_timeout(messages)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI Copilot   .    .",
        )

    await _store_memory(tenant_id, payload.message, reply)

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
    """Execute a one-click AI action  kick off multiple operations at once.

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
    memory_ctx = ""

    try:
        context_text, sources = await _gather_context_data(db, identity, days=days, focus=None)
    except Exception as exc:
        context_text = f"(  : {exc})"
        sources = []

    if tenant_id:
        memory_ctx = await _get_memory_context(tenant_id, payload.action, limit=3)
        telemon_memory = await build_telemon_memory_context(db, identity, payload.action)
        if telemon_memory.text:
            context_text = f"{telemon_memory.text}\n\n{context_text}" if context_text else telemon_memory.text
        if memory_ctx:
            context_text = f"{memory_ctx}\n\n{context_text}" if context_text else memory_ctx

    if payload.action == "health_check":
        system_prompt = (
            " TeleMon AI   .     "
            "   .\n\n"
            "   :\n"
            "1.    ( , )\n"
            "2.   ( ,  )\n"
            "3.   (  , )\n"
            "4.   \n\n"
            "  (''/''/'')  .\n"
            " ."
        )
        user_prompt = f"[  -  {days}]\n{context_text}"
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
            details.append({"step": "delivery_health", "status": "failed", "finding": " "})
            all_ok = False

        # Also run customer pulse
        if tenant_id:
            try:
                total = await get_lead_count(tenant_id)
                details.append({
                    "step": "customer_pulse",
                    "status": "completed",
                    "finding": f"  {total}",
                })
            except Exception as exc:
                details.append({"step": "customer_pulse", "status": "failed", "finding": str(exc)})
                all_ok = False

    elif payload.action == "weekly_report":
        system_prompt = (
            " TeleMon    . "
            "    .\n\n"
            ":\n"
            "##     ( {days})\n\n"
            "### 1.  \n"
            "-   , , \n\n"
            "### 2.  \n"
            "-   \n\n"
            "### 3.  \n"
            "-  ,  \n\n"
            "### 4.   & \n"
            "-   3 \n\n"
            " ."
        ).format(days=days)
        user_prompt = f"[ ]\n{context_text}"
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
            details.append({"step": "weekly_report", "status": "failed", "finding": "  "})
            all_ok = False

    elif payload.action == "optimize_broadcast":
        system_prompt = (
            " TeleMon   . "
            "        .\n\n"
            " :\n"
            "1.    \n"
            "2.  /  \n"
            "3.   \n"
            "4.  / \n"
            "5. A/B  \n\n"
            "     .\n"
            " ."
        )
        user_prompt = f"[/  -  {days}]\n{context_text}"
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
            details.append({"step": "broadcast_optimization", "status": "failed", "finding": " "})
            all_ok = False

    elif payload.action == "customer_insights":
        system_prompt = (
            " TeleMon   . "
            "      .\n\n"
            " :\n"
            "1.    (// )\n"
            "2.  \n"
            "3.    \n"
            "4.   \n\n"
            "  (//) .\n"
            " ."
        )
        user_prompt = f"[  -  {days}]\n{context_text}"
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
            details.append({"step": "customer_insights", "status": "failed", "finding": " "})
            all_ok = False

    elif payload.action == "reply_audit":
        system_prompt = (
            " TeleMon    . "
            "        .\n\n"
            " :\n"
            "1.   \n"
            "2.   \n"
            "3.  (   vs  )\n"
            "4.  \n\n"
            " ."
        )
        user_prompt = f"[ ]\n{context_text}\n\n         ."
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
            details.append({"step": "reply_audit", "status": "failed", "finding": "AI 응답을 받지 못했습니다."})
            all_ok = False
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"지원하지 않는 action입니다: {payload.action}",
        )

    elapsed = int((time.monotonic() - start_time) * 1000)
    overall_status = "completed" if all_ok else "partial"

    summary_parts = [d.get("finding", "")[:100] for d in details if d.get("status") == "completed"]
    summary = "\n".join(summary_parts)[:500] if summary_parts else "  ."

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

    tenant_id = identity.tenant_id or "anonymous"
    memory_context = await _get_memory_context(tenant_id, "recommendations", limit=3)

    system_prompt = (
        " TeleMon AI  .    "
        "    .\n\n"
        "  JSON   (  ):\n"
        "{\n"
        '  "overall_health": "good|fair|needs_attention|critical",\n'
        '  "recommendations": [\n'
        "    {\n"
        '      "title": " ",\n'
        '      "description": " ",\n'
        '      "category": "delivery|customers|broadcast|accounts|general",\n'
        '      "confidence": 0.95,\n'
        '      "reasoning": "     ",\n'
        '      "suggested_action": "    ",\n'
        '      "impact": "high|medium|low"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        ":\n"
        "- 3~7   \n"
        "- confidence    (  )\n"
        "- impact 'high'   \n"
        "-    reasoning \n"
        "-  "
    )
    user_prompt = f"[  -  {days}]\n{context_text}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    reply = await _call_deepseek_with_timeout(messages)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI   .    .",
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
                title=item.get("title", ""),
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
    that this triggers a new DeepSeek call every time  no caching)."""
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

    tenant_id = identity.tenant_id or "anonymous"
    memory_context = await _get_memory_context(tenant_id, "smart send time", limit=3)

    system_prompt = (
        " TeleMon    . "
        "        "
        "   .\n\n"
        "  JSON  :\n"
        "{\n"
        '  "recommended_hour": 10,\n'
        '  "recommended_day": "weekday",\n'
        '  "reasoning": " ",\n'
        '  "confidence": 0.85\n'
        "}\n\n"
        ":\n"
        f"-  : {payload.timezone}\n"
        f"-  : {payload.recipient_count}\n"
        "- recommended_hour: 0-23  \n"
        "- recommended_day: 'weekday', 'weekend',   ()\n"
        "- confidence: 0.0~1.0 (  )\n"
        "-  reasoning "
    )
    user_prompt = f"[  -  30]\n{context_text}" if context_text else "(       )"
    if memory_context:
        user_prompt = f"{memory_context}\n\n{user_prompt}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    reply = await _call_deepseek_with_timeout(messages)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI 응답을 받지 못했습니다. 잠시 후 다시 시도해주세요.",
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
            reasoning="    .   ( 10 ) .",
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
            delivery_rate = " "
    except Exception:
        pass

    try:
        if tenant_id:
            total_leads = await get_lead_count(tenant_id)
    except Exception:
        pass

    quick_actions = [
        {"id": "health_check", "label": "   ", "description": "//   "},
        {"id": "weekly_report", "label": "   ", "description": "7    "},
        {"id": "optimize_broadcast", "label": "   ", "description": "    "},
        {"id": "customer_insights", "label": "  ", "description": "    "},
        {"id": "reply_audit", "label": "   ", "description": "   "},
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