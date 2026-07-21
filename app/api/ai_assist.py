"""AI Assist API — LLM-powered operational features.

All endpoints reuse ``_call_deepseek`` from ``app.services.ai_chat_service`` so the
same DeepSeek configuration, provider, and quota model applies. No new provider
or separate API key is introduced.

Endpoints are suggestion-only: nothing in this router sends a Telegram message
or creates a broadcast — the frontend takes the drafted content and calls the
existing create-broadcast / send flows itself.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Identity, get_current_identity, require_admin
from app.crud.ai_broadcast_draft import create_draft, list_recent_drafts
from app.crud.ai_ops_report import list_recent_reports
from app.database import get_db
from app.services.ai_analysis_service import DELIVERY_SYSTEM_PROMPT, analyze_text_report
from app.services.ai_chat_service import _call_deepseek
from app.services.ai_ops_service import generate_and_store_ops_report
from app.services.ai_reply_service import generate_reply_suggestion
from app.services.lead_capture import get_lead_count, get_leads
from app.services.telemon_memory_service import build_telemon_memory_context

router = APIRouter(prefix="/api/ai", tags=["ai-assist"])


# ─── Request / Response schemas ──────────────────────────────────────


class GenerateMessageRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000, description="사용자의 메시지 작성 요청 (목적, 대상, 톤 등)")


class GenerateMessageResponse(BaseModel):
    content: str


class AnalyzeDeliveryRequest(BaseModel):
    summary: str = Field(..., description="전달 분석 요약 데이터 (JSON text)")
    failures: str = Field("", description="실패 분석 데이터 (JSON text)")
    accounts: str = Field("", description="계정 성과 데이터 (JSON text)")
    days: int = Field(30, ge=1, le=365, description="분석 기간(일)")


class AnalyzeDeliveryResponse(BaseModel):
    report: str
    anomalies: list[str] = []


class SuggestReplyRequest(BaseModel):
    incoming_message: str = Field(..., min_length=1, max_length=4096, description="고객이 보낸 원본 메시지")
    conversation_context: str | None = Field(default=None, max_length=2000, description="이전 대화 맥락 (선택)")


class SuggestReplyResponse(BaseModel):
    reply: str


class ToneVariant(BaseModel):
    tone: str
    content: str
    reasoning: str


class OptimizeBroadcastRequest(BaseModel):
    original_message: str = Field(..., min_length=1, max_length=4096, description="최적화할 원본 메시지")
    target_audience: str | None = Field(default=None, max_length=500, description="대상 청중 설명 (선택)")
    goal: str | None = Field(default=None, max_length=500, description="목표 (예: 클릭 유도, 참여율 증가)")


class OptimizeBroadcastResponse(BaseModel):
    improved_version: str
    variations: list[ToneVariant] = []


class BroadcastRecipientCandidate(BaseModel):
    chat_id: str
    name: str = ""


class GenerateBroadcastRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000, description="발송 목적/내용 요청")
    candidate_recipients: list[BroadcastRecipientCandidate] = Field(
        default_factory=list, max_length=200,
        description="후보 발송 대상 목록. AI는 이 목록 안에서만 추천함.",
    )


class GenerateBroadcastResponse(BaseModel):
    message: str
    recommended_chat_ids: list[str] = []
    reasoning: str = ""


class AiBroadcastDraftRead(BaseModel):
    id: str
    prompt: str
    message: str
    recommended_chat_ids: list[str]
    reasoning: str
    created_at: str


class AnalyzeCustomersRequest(BaseModel):
    tenant_id: str | None = Field(default=None, description="분석할 tenant. 관리자는 필수, 일반 사용자는 본인 tenant로 고정.")
    days: int = Field(30, ge=1, le=365, description="분석 기간(일)")


class AnalyzeCustomersResponse(BaseModel):
    report: str
    insights: list[str] = []


class AnalyzeChatsRequest(BaseModel):
    tenant_id: str | None = Field(default=None, description="분석할 tenant")
    days: int = Field(30, ge=1, le=365, description="분석 기간(일)")


class AnalyzeChatsResponse(BaseModel):
    report: str
    active_groups: list[str] = []
    inactive_groups: list[str] = []
    recommended_targets: list[str] = []


class SendTimeRecommendationResponse(BaseModel):
    recommended_hour_utc: int
    recommended_day: str
    reasoning: str
    best_times: list[str]


class DashboardSummaryResponse(BaseModel):
    summary: str
    risks: list[str] = []
    opportunities: list[str] = []
    recommended_actions: list[str] = []


class AiOpsReportRead(BaseModel):
    id: str
    report: str
    anomalies: list[str]
    created_at: str


# ─── Endpoints ───────────────────────────────────────────────────────


@router.post("/generate-message", response_model=GenerateMessageResponse)
async def api_generate_message(
    payload: GenerateMessageRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> GenerateMessageResponse:
    """Generate a Telegram broadcast message draft using DeepSeek."""
    memory = await build_telemon_memory_context(db, identity, payload.prompt)
    messages = [
        {
            "role": "system",
            "content": (
                "너는 TeleMon 서비스의 메시지 작성 도우미야. "
                "사용자의 요청에 따라 텔레그램 그룹/채널 발송용 메시지를 작성해줘.\n\n"
                "아래 TeleMon 전용 Memory를 반드시 참고해 더 나은 문구를 만든다.\n"
                "사용자가 재홍보/다시 요청하면 '지난 성과 높은 글을 참고했다'는 취지를 자연스럽게 포함한다.\n\n"
                "규칙:\n"
                "- 결과는 반드시 한국어로 출력\n"
                "- 메시지만 출력 (설명/코멘트 없이)\n"
                "- {{name}}, {{phone}}, {{count}} 변수는 그대로 유지\n"
                "- 2000자 이내로 작성\n"
                "- 필요시 이모지를 적절히 사용\n"
                "- 톤은 기본적으로 친근하고 전문적으로"
            ),
        },
        {"role": "system", "content": memory.text or "[TeleMon 전용 Memory] 데이터 없음"},
        {"role": "user", "content": payload.prompt},
    ]
    reply = await _call_deepseek(messages)
    if reply is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="AI 메시지 생성에 실패했습니다.")
    return GenerateMessageResponse(content=reply.strip())


@router.post("/analyze-delivery", response_model=AnalyzeDeliveryResponse)
async def api_analyze_delivery(
    payload: AnalyzeDeliveryRequest,
    identity: Identity = Depends(get_current_identity),
) -> AnalyzeDeliveryResponse:
    data_lines = [f"[요약] {payload.summary}"]
    if payload.failures:
        data_lines.append(f"[실패 분석] {payload.failures}")
    if payload.accounts:
        data_lines.append(f"[계정 성과] {payload.accounts}")
    data_lines.append(f"[분석 기간] 최근 {payload.days}일")
    report, anomalies = await analyze_text_report(DELIVERY_SYSTEM_PROMPT, "\n".join(data_lines))
    if report is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="AI 분석 생성에 실패했습니다.")
    return AnalyzeDeliveryResponse(report=report, anomalies=anomalies)


# ── 1. AI Reply Assistant (enhanced) ─────────────────────────────────


@router.post("/suggest-reply", response_model=SuggestReplyResponse)
async def api_suggest_reply(
    payload: SuggestReplyRequest,
    identity: Identity = Depends(get_current_identity),
) -> SuggestReplyResponse:
    """Draft a reply suggestion with optional conversation context.
    
    The system prompt uses the conversation context to craft more relevant replies.
    Suggestion-only — the operator reviews and sends manually.
    """
    system_prompt = (
        "너는 TeleMon 서비스의 답장 작성 도우미야. "
        "고객이 보낸 메시지에 대해 자연스럽고 도움이 되는 답장 초안을 작성해줘.\n\n"
        "규칙:\n"
        "- 결과는 반드시 한국어로 출력\n"
        "- 답장 내용만 출력 (설명/코멘트 없이)\n"
        "- 2000자 이내로 작성\n"
        "- 톤은 친근하고 전문적으로\n"
        "- 이 답장은 사람이 검토 후 직접 전송하므로, 확신이 없으면 그렇게 드러내도 좋음"
    )
    user_content = payload.incoming_message
    if payload.conversation_context:
        user_content = f"[대화 맥락]\n{payload.conversation_context}\n\n[고객 메시지]\n{payload.incoming_message}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    reply = await _call_deepseek(messages)
    if reply is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="AI 답장 생성에 실패했습니다.")
    return SuggestReplyResponse(reply=reply.strip())


# ── 2. AI Broadcast Assistant ────────────────────────────────────────


@router.post("/optimize-broadcast", response_model=OptimizeBroadcastResponse)
async def api_optimize_broadcast(
    payload: OptimizeBroadcastRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> OptimizeBroadcastResponse:
    """Improve a broadcast message for higher engagement.
    
    Returns an improved version plus 3 tone variations (professional, casual,
    urgent) each with reasoning explaining why that version is recommended.
    """
    audience_line = f"대상 청중: {payload.target_audience}" if payload.target_audience else ""
    goal_line = f"목표: {payload.goal}" if payload.goal else ""

    memory = await build_telemon_memory_context(db, identity, payload.original_message)
    system_prompt = (
        "너는 TeleMon 서비스의 메시지 최적화 도우미야. "
        "사용자가 작성한 발송 메시지를 분석하고 더 높은 참여율을 위한 개선 버전을 제시해줘.\n\n"
        "반드시 TeleMon 전용 Memory의 과거 고성과 문구 패턴을 반영해 개선한다.\n\n"
        "반드시 아래 JSON 형식으로만 응답 (다른 텍스트 없이):\n"
        '{\n'
        '  "improved": "개선된 메시지 전체",\n'
        '  "variations": [\n'
        '    {"tone": "전문적인", "content": "...", "reasoning": "이 버전을 추천하는 이유"}, \n'
        '    {"tone": "친근한", "content": "...", "reasoning": "..."}, \n'
        '    {"tone": "긴급한", "content": "...", "reasoning": "..."}\n'
        '  ]\n'
        '}\n\n'
        "규칙:\n"
        "- {{name}}, {{phone}}, {{count}} 변수는 그대로 유지\n"
        "- 각 버전마다 추천 이유를 구체적으로 설명 (참여율, 클릭률, 가독성 등)\n"
        "- 한국어로 출력"
    )
    user_content = f"[원본 메시지]\n{payload.original_message}\n\n{audience_line}\n{goal_line}".strip()

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": memory.text or "[TeleMon 전용 Memory] 데이터 없음"},
        {"role": "user", "content": user_content},
    ]
    reply = await _call_deepseek(messages)
    if reply is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="AI 메시지 최적화에 실패했습니다.")

    try:
        parsed = json.loads(reply.strip())
        improved = str(parsed.get("improved", reply)).strip()
        variations_raw = parsed.get("variations", [])
        variations = [
            ToneVariant(tone=v.get("tone", ""), content=v.get("content", ""), reasoning=v.get("reasoning", ""))
            for v in variations_raw if isinstance(v, dict)
        ]
    except (json.JSONDecodeError, TypeError, ValueError):
        improved = reply.strip()
        variations = []

    return OptimizeBroadcastResponse(improved_version=improved, variations=variations)


@router.post("/generate-broadcast", response_model=GenerateBroadcastResponse)
async def api_generate_broadcast(
    payload: GenerateBroadcastRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
) -> GenerateBroadcastResponse:
    """Draft a broadcast message and recommend recipients from candidates."""
    candidates_text = (
        "\n".join(f"- {c.chat_id}: {c.name}" for c in payload.candidate_recipients)
        if payload.candidate_recipients else "(제공된 후보 없음)"
    )
    memory = await build_telemon_memory_context(db, identity, payload.prompt)
    system_prompt = (
        "너는 TeleMon 서비스의 발송(Broadcast) 도우미야. "
        "사용자의 요청에 맞는 발송 메시지를 작성하고, 아래 '후보 대상 목록'에 있는 chat_id 중에서만 "
        "적합한 대상을 추천해줘. 목록에 없는 chat_id는 절대 만들어내지 마.\n\n"
        "또한 TeleMon 전용 Memory에서 과거 성과가 높았던 문구 패턴을 참고해 품질을 개선한다.\n\n"
        "반드시 아래 JSON 형식으로만 응답 (다른 텍스트 없이):\n"
        '{"message": "발송 메시지", "recommended_chat_ids": ["id1", "id2"], "reasoning": "선정 이유 한 줄"}\n\n'
        f"[후보 대상 목록]\n{candidates_text}"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": memory.text or "[TeleMon 전용 Memory] 데이터 없음"},
        {"role": "user", "content": payload.prompt},
    ]
    reply = await _call_deepseek(messages)
    if reply is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="AI 발송 생성에 실패했습니다.")

    candidate_ids = {c.chat_id for c in payload.candidate_recipients}
    try:
        parsed = json.loads(reply.strip())
        message = str(parsed["message"]).strip()
        recommended = [cid for cid in parsed.get("recommended_chat_ids", []) if cid in candidate_ids]
        reasoning = str(parsed.get("reasoning", ""))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        message = reply.strip()
        recommended = []
        reasoning = ""

    await create_draft(db, prompt=payload.prompt, message=message, recommended_chat_ids=recommended, reasoning=reasoning)
    return GenerateBroadcastResponse(message=message, recommended_chat_ids=recommended, reasoning=reasoning)


@router.get("/broadcast-drafts", response_model=list[AiBroadcastDraftRead], dependencies=[Depends(require_admin)])
async def api_list_broadcast_drafts(limit: int = 20, db: AsyncSession = Depends(get_db)):
    drafts = await list_recent_drafts(db, limit=limit)
    return [
        AiBroadcastDraftRead(
            id=d.id, prompt=d.prompt, message=d.message,
            recommended_chat_ids=json.loads(d.recommended_chat_ids_json or "[]"),
            reasoning=d.reasoning, created_at=d.created_at.isoformat(),
        ) for d in drafts
    ]


# ── 3. AI Customer Insights ──────────────────────────────────────────


@router.post("/analyze-customers", response_model=AnalyzeCustomersResponse)
async def api_analyze_customers(
    payload: AnalyzeCustomersRequest, identity: Identity = Depends(get_current_identity)
) -> AnalyzeCustomersResponse:
    """Analyze tenant's lead/CRM data — active/inactive detection, engagement levels."""
    tenant_id = _resolve_tenant_id(payload.tenant_id, identity)
    leads = await get_leads(tenant_id, limit=200)
    total = await get_lead_count(tenant_id)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=payload.days)

    active_in_window = sum(1 for lead in leads if lead.last_interaction and lead.last_interaction >= cutoff)
    stale_beyond_window = sum(1 for lead in leads if lead.last_interaction and lead.last_interaction < cutoff)
    no_interaction = sum(1 for lead in leads if not lead.last_interaction)
    engaged_3plus = sum(1 for lead in leads if (lead.total_messages or 0) >= 3)

    summary = {
        "total_leads": total, "sampled": len(leads),
        "active_in_window": active_in_window, "stale_beyond_window": stale_beyond_window,
        "no_interaction": no_interaction, "engaged_3plus_messages": engaged_3plus,
    }
    sample_lines = [
        f"- {lead.telegram_username or lead.first_name or lead.telegram_user_id}: "
        f"{lead.total_messages or 0}건, 마지막 {lead.last_interaction or '없음'}"
        for lead in leads[:30]
    ]

    system_prompt = (
        "너는 TeleMon 서비스의 AI 고객 분석가야. "
        "고객/리드 데이터를 보고 운영자에게 의미 있는 인사이트를 제공해줘.\n\n"
        "응답 형식:\n"
        "1. 먼저 전반적인 고객 현황을 3-4문장으로 요약\n"
        "2. 주목할 만한 인사이트(insights)를 \";\"로 구분해서 나열 (이탈 위험, 세그먼트 기회, 참여도 변화)\n"
        "3. 각 인사이트에 대해 이유와 제안 액션을 간단히 제시\n\n"
        "주의:\n"
        "- 한국어로 답변\n"
        "- 구체적인 수치를 포함\n"
        "- 운영자가 바로 조치할 수 있는 액션 아이템 위주로"
    )
    user_prompt = "\n".join([
        f"[고객 요약] {json.dumps(summary, ensure_ascii=False)}",
        f"[샘플 (최대 30건)]\n" + "\n".join(sample_lines) if sample_lines else "[샘플] (리드 없음)",
        f"[분석 기간] 최근 {payload.days}일",
    ])
    report, insights = await analyze_text_report(system_prompt, user_prompt)
    if report is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="AI 고객 분석에 실패했습니다.")
    return AnalyzeCustomersResponse(report=report, insights=insights)


@router.post("/analyze-chats", response_model=AnalyzeChatsResponse)
async def api_analyze_chats(
    payload: AnalyzeChatsRequest, identity: Identity = Depends(get_current_identity)
) -> AnalyzeChatsResponse:
    """Analyze chats/groups for a tenant.
    
    Detects active/inactive groups and recommends the best target audience
    based on engagement metrics and group characteristics.
    """
    tenant_id = _resolve_tenant_id(payload.tenant_id, identity)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=payload.days)

    from sqlalchemy import select, func as sa_func
    from app.models.message_log import MessageLog
    from app.models.account import Account
    from app.database import async_session_maker

    async with async_session_maker() as session:
        result = await session.execute(
            select(Account.id).where(Account.tenant_id == tenant_id)
        )
        account_ids = [r[0] for r in result.all()]

    group_stats: dict[str, dict] = {}
    async with async_session_maker() as session:
        for aid in account_ids:
            rows = await session.execute(
                select(
                    MessageLog.recipient,
                    sa_func.count(MessageLog.id),
                    sa_func.max(MessageLog.created_at),
                    sa_func.sum(sa_func.cast(MessageLog.success, sa_func.Integer())),
                ).where(
                    MessageLog.account_id == aid,
                    MessageLog.source == "broadcast",
                ).group_by(MessageLog.recipient)
            )
            for recipient, count, last_at, successes in rows:
                gid = str(recipient)
                if gid not in group_stats:
                    group_stats[gid] = {"total": 0, "last": None, "successes": 0}
                group_stats[gid]["total"] += count
                group_stats[gid]["successes"] += (successes or 0)
                if last_at and (group_stats[gid]["last"] is None or last_at > group_stats[gid]["last"]):
                    group_stats[gid]["last"] = last_at

    active = [gid for gid, s in group_stats.items() if s["last"] and s["last"] >= cutoff]
    inactive = [gid for gid, s in group_stats.items() if s["last"] and s["last"] < cutoff]
    high_engagement = sorted(
        [gid for gid in active],
        key=lambda g: group_stats[g]["successes"] / max(group_stats[g]["total"], 1),
        reverse=True,
    )[:10]

    summary_lines = [
        f"- 총 {len(group_stats)}개 그룹/채널 분석",
        f"- 활성: {len(active)}개, 휴면: {len(inactive)}개",
        f"- 높은 참여율 그룹: {len(high_engagement)}개",
    ]
    if active:
        summary_lines.append(f"- 추천 발송 대상 (활성): {', '.join(active[:5])}")
    if high_engagement:
        summary_lines.append(f"- 최적 타겟 (참여율 상위): {', '.join(high_engagement[:5])}")

    system_prompt = (
        "너는 TeleMon 서비스의 AI 그룹 분석가야. "
        "텔레그램 그룹/채널 데이터를 분석하고 마케팅 인사이트를 제공해줘.\n\n"
        "응답 형식:\n"
        "1. 전체 그룹 현황을 3-4문장으로 요약\n"
        "2. 활성/휴면 그룹 현황 분석\n"
        "3. 최적의 발송 대상 추천 (이유 포함)\n\n"
        "주의: 한국어로 답변, 구체적인 수치 포함"
    )
    user_content = "\n".join(summary_lines)
    report, _ = await analyze_text_report(system_prompt, user_content)

    return AnalyzeChatsResponse(
        report=report or "분석 결과를 생성할 수 없습니다.",
        active_groups=active[:10],
        inactive_groups=inactive[:10],
        recommended_targets=high_engagement,
    )


# ── 4. AI Send Time Optimization ─────────────────────────────────────


@router.post("/send-time", response_model=SendTimeRecommendationResponse)
async def api_recommend_send_time(
    identity: Identity = Depends(get_current_identity),
) -> SendTimeRecommendationResponse:
    """Recommend the best sending time based on historical delivery data.
    
    Analyzes MessageLog delivery timestamps to find peak engagement windows
    and recommends the optimal hour and day for broadcast sends.
    """
    tenant_id = _resolve_tenant_id(None, identity) if identity.kind != "admin" else None

    from sqlalchemy import select, func as sa_func, extract
    from app.models.message_log import MessageLog
    from app.models.account import Account
    from app.database import async_session_maker

    hour_stats: dict[int, int] = {}
    day_stats: dict[str, int] = {}
    total = 0

    async with async_session_maker() as db:
        query = select(
            extract('hour', MessageLog.created_at),
            MessageLog.recipient,
        ).where(
            MessageLog.source == "broadcast",
            MessageLog.success.is_(True),
        )
        if tenant_id:
            account_ids_q = select(Account.id).where(Account.tenant_id == tenant_id)
            query = query.where(MessageLog.account_id.in_(account_ids_q))

        rows = await db.execute(query)
        for hour, _ in rows:
            h = int(hour) if hour is not None else -1
            if h >= 0:
                hour_stats[h] = hour_stats.get(h, 0) + 1
                total += 1

    if total == 0:
        return SendTimeRecommendationResponse(
            recommended_hour_utc=9, recommended_day="평일",
            reasoning="과거 데이터가 충분하지 않아 기본값(오전 9시)을 추천합니다.",
            best_times=["오전 9시 (UTC)", "오전 11시 (UTC)", "오후 2시 (UTC)"],
        )

    sorted_hours = sorted(hour_stats.items(), key=lambda x: -x[1])
    top_hour = sorted_hours[0][0] if sorted_hours else 9

    hour_labels = []
    for h, cnt in sorted_hours[:5]:
        local_h = (h + 9) % 24  # UTC+9 (KST)
        period = "오전" if local_h < 12 else "오후"
        hour12 = local_h if local_h <= 12 else local_h - 12
        hour12 = 12 if hour12 == 0 else hour12
        hour_labels.append(f"{period} {hour12}시 ({cnt}건)")

    data_summary = f"분석된 총 발송: {total}건, 최다 발송 시간대 (UTC): {sorted_hours[:5]}"
    if top_hour >= 0:
        local_top = (top_hour + 9) % 24
        period = "오전" if local_top < 12 else "오후"
        hour12 = local_top if local_top <= 12 else local_top - 12
        hour12 = 12 if hour12 == 0 else hour12
        readable = f"{period} {hour12}시"

    system_prompt = (
        "너는 TeleMon 서비스의 AI 발송 시간 최적화 분석가야. "
        "과거 발송 데이터를 분석하여 최적의 발송 시간대를 추천해줘.\n\n"
        "응답 형식:\n"
        "1. 데이터 분석 결과 요약\n"
        "2. 추천 시간대와 이유\n"
        "3. 운영자가 바로 적용할 수 있는 구체적인 제안\n\n"
        "한국어로 답변, 구체적인 수치 포함."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"[발송 시간 데이터]\n{data_summary}"},
    ]
    reply = await _call_deepseek(messages)
    reasoning = reply.strip() if reply else f"과거 {total}건 데이터 기준, UTC {top_hour}시 (한국 {readable})가 가장 발송량이 많았습니다."

    return SendTimeRecommendationResponse(
        recommended_hour_utc=top_hour,
        recommended_day="평일",
        reasoning=reasoning,
        best_times=hour_labels,
    )


# ── 5. AI Dashboard Summary ──────────────────────────────────────────


@router.post("/dashboard-summary", response_model=DashboardSummaryResponse)
async def api_dashboard_summary(
    identity: Identity = Depends(get_current_identity),
) -> DashboardSummaryResponse:
    """Daily AI summary with risks, opportunities, and recommended actions.
    
    Aggregates data from multiple sources:
    - Broadcast history: failed/recent broadcasts
    - Account health: inactive/banned accounts
    - Delivery analytics: success rates, trends
    - Returns a natural-language summary with actionable recommendations.
    """
    tenant_id = _resolve_tenant_id(None, identity) if identity.kind != "admin" else None

    from sqlalchemy import select, func as sa_func
    from app.models.broadcast import Broadcast
    from app.models.account import Account
    from app.models.message_log import MessageLog
    from app.database import async_session_maker

    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    broadcast_stats = {"total_recent": 0, "failed_24h": 0, "sent_24h": 0}
    account_stats: dict[str, int] = {}
    delivery_stats = {"last_24h_success": 0, "last_24h_fail": 0, "last_7d_success": 0, "last_7d_fail": 0}

    async with async_session_maker() as db:
        bc_q = select(Broadcast.account_id, Broadcast.status, Broadcast.created_at)
        acc_q = select(Account.id, Account.status, Account.tenant_id)
        ml_q = select(MessageLog.success, MessageLog.created_at, MessageLog.account_id)

        rows = await db.execute(bc_q)
        for account_id, status, created_at in rows:
            if tenant_id:
                acc_check = await db.execute(select(Account.tenant_id).where(Account.id == account_id))
                t_id = acc_check.scalar_one_or_none()
                if t_id != tenant_id:
                    continue
            broadcast_stats["total_recent"] += 1
            if created_at and created_at >= yesterday and status == "failed":
                broadcast_stats["failed_24h"] += 1
            if created_at and created_at >= yesterday and status == "sent":
                broadcast_stats["sent_24h"] += 1

        acc_rows = await db.execute(acc_q)
        for acc_id, status, t_id in rows:
            if tenant_id and t_id != tenant_id:
                continue
            account_stats[status or "unknown"] = account_stats.get(status or "unknown", 0) + 1

        ml_rows = await db.execute(ml_q)
        for success, created_at, account_id in ml_rows:
            if tenant_id:
                acc_check = await db.execute(select(Account.tenant_id).where(Account.id == account_id))
                t_id = acc_check.scalar_one_or_none()
                if t_id != tenant_id:
                    continue
            if created_at:
                if created_at >= yesterday:
                    if success:
                        delivery_stats["last_24h_success"] += 1
                    else:
                        delivery_stats["last_24h_fail"] += 1
                if created_at >= week_ago:
                    if success:
                        delivery_stats["last_7d_success"] += 1
                    else:
                        delivery_stats["last_7d_fail"] += 1

    data_lines = [
        f"[발송 현황] 최근 전체: {broadcast_stats['total_recent']}건, 24시간 내 실패: {broadcast_stats['failed_24h']}건, 성공: {broadcast_stats['sent_24h']}건",
        f"[계정 상태] {json.dumps(account_stats, ensure_ascii=False)}",
        f"[전달율] 24시간: 성공 {delivery_stats['last_24h_success']} / 실패 {delivery_stats['last_24h_fail']}, 7일: 성공 {delivery_stats['last_7d_success']} / 실패 {delivery_stats['last_7d_fail']}",
    ]

    system_prompt = (
        "너는 TeleMon 서비스의 AI 대시보드 분석가야. "
        "운영 데이터를 분석하여 대시보드 요약을 제공해줘.\n\n"
        "반드시 아래 JSON 형식으로만 응답:\n"
        '{\n'
        '  "summary": "전체 현황 3-4문장 요약",\n'
        '  "risks": ["위험 항목1", "위험 항목2"],\n'
        '  "opportunities": ["기회 항목1", "기회 항목2"],\n'
        '  "recommended_actions": ["권장 조치1", "권장 조치2"]\n'
        '}\n\n'
        "규칙:\n"
        "- 모든 내용은 한국어로\n"
        "- 위험: 실패, 차단, 비활성 계정 등\n"
        "- 기회: 높은 참여율, 성공률 개선, 활성 그룹 등\n"
        "- 권장 조치: 운영자가 바로 실행할 수 있는 구체적인 액션"
    )
    user_content = "\n".join(data_lines)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    reply = await _call_deepseek(messages)

    if reply is None:
        return DashboardSummaryResponse(
            summary="데이터를 분석할 수 없습니다.",
            risks=[], opportunities=[], recommended_actions=["잠시 후 다시 시도해주세요."],
        )

    try:
        parsed = json.loads(reply.strip())
        return DashboardSummaryResponse(
            summary=str(parsed.get("summary", "")),
            risks=[str(r) for r in parsed.get("risks", []) if r],
            opportunities=[str(o) for o in parsed.get("opportunities", []) if o],
            recommended_actions=[str(a) for a in parsed.get("recommended_actions", []) if a],
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return DashboardSummaryResponse(
            summary=reply.strip(),
            risks=[], opportunities=[], recommended_actions=[],
        )


# ── Shared helpers and existing endpoints ────────────────────────────


def _resolve_tenant_id(payload_tenant_id: str | None, identity: Identity) -> str:
    """Resolve tenant ID from payload or identity."""
    if identity.kind == "admin":
        if not payload_tenant_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="관리자는 tenant_id를 지정해야 합니다.")
        return payload_tenant_id
    if not identity.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="연동된 tenant가 없습니다.")
    return identity.tenant_id


@router.get("/ops-reports", response_model=list[AiOpsReportRead], dependencies=[Depends(require_admin)])
async def api_list_ops_reports(limit: int = 20, db: AsyncSession = Depends(get_db)):
    reports = await list_recent_reports(db, limit=limit)
    return [
        AiOpsReportRead(
            id=r.id, report=r.report, anomalies=json.loads(r.anomalies_json or "[]"),
            created_at=r.created_at.isoformat(),
        ) for r in reports
    ]


@router.post("/ops-reports/generate", response_model=AiOpsReportRead, dependencies=[Depends(require_admin)])
async def api_generate_ops_report_now():
    report = await generate_and_store_ops_report()
    if report is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="리포트 생성에 실패했습니다.")
    return AiOpsReportRead(
        id=report.id, report=report.report,
        anomalies=json.loads(report.anomalies_json or "[]"),
        created_at=report.created_at.isoformat(),
    )