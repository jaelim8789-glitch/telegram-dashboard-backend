"""AI Assist API — LLM-powered operational features.

Endpoints reuse ``_call_deepseek`` from ``app.services.ai_chat_service`` (via
``app.services.ai_analysis_service`` / ``app.services.ai_reply_service`` for
the shared logic) so the same DeepSeek configuration, provider, and quota
model applies. No new provider or separate API key is introduced.

All endpoints are gated by the standard ``require_api_key_or_admin`` dependency
so no bot/Payment/auth flow is touched. Reply/broadcast generation here is
suggestion-only: nothing in this router sends a Telegram message or creates a
broadcast — the frontend takes the drafted content and calls the existing
create-broadcast / send flows itself.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.crud.ai_ops_report import list_recent_reports
from app.database import get_db
from app.services.ai_analysis_service import DELIVERY_SYSTEM_PROMPT, analyze_text_report
from app.services.ai_chat_service import _call_deepseek
from app.services.ai_reply_service import generate_reply_suggestion

router = APIRouter(prefix="/api/ai", tags=["ai-assist"])

# ─── Request / Response schemas ──────────────────────────────────────


class GenerateMessageRequest(BaseModel):
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="사용자의 메시지 작성 요청 (목적, 대상, 톤 등)",
    )


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


class SuggestReplyResponse(BaseModel):
    reply: str


class BroadcastRecipientCandidate(BaseModel):
    chat_id: str
    name: str = ""


class GenerateBroadcastRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000, description="발송 목적/내용 요청")
    candidate_recipients: list[BroadcastRecipientCandidate] = Field(
        default_factory=list,
        description="후보 발송 대상 목록 (프론트에서 이미 보유한 그룹/리드 목록). AI는 이 목록 안에서만 추천함.",
    )


class GenerateBroadcastResponse(BaseModel):
    message: str
    recommended_chat_ids: list[str] = []
    reasoning: str = ""


class AnalyzeCustomersRequest(BaseModel):
    summary: str = Field(..., description="고객/리드 요약 데이터 (JSON text)")
    segments: str = Field("", description="세그먼트/태그 분포 데이터 (JSON text)")
    days: int = Field(30, ge=1, le=365, description="분석 기간(일)")


class AnalyzeCustomersResponse(BaseModel):
    report: str
    insights: list[str] = []


class AiOpsReportRead(BaseModel):
    id: str
    report: str
    anomalies: list[str]
    created_at: str


# ─── Endpoints ───────────────────────────────────────────────────────


@router.post("/generate-message", response_model=GenerateMessageResponse)
async def api_generate_message(
    payload: GenerateMessageRequest,
) -> GenerateMessageResponse:
    """Generate a Telegram broadcast message draft using DeepSeek.

    The system prompt instructs the model to act as a TeleMon message
    writing assistant.  The user prompt is the plain-text request.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "너는 TeleMon 서비스의 메시지 작성 도우미야. "
                "사용자의 요청에 따라 텔레그램 그룹/채널 발송용 메시지를 작성해줘.\n\n"
                "규칙:\n"
                "- 결과는 반드시 한국어로 출력\n"
                "- 메시지만 출력 (설명/코멘트 없이)\n"
                "- {{name}}, {{phone}}, {{count}} 변수는 그대로 유지\n"
                "- 2000자 이내로 작성\n"
                "- 필요시 이모지를 적절히 사용\n"
                "- 톤은 기본적으로 친근하고 전문적으로"
            ),
        },
        {"role": "user", "content": payload.prompt},
    ]

    reply = await _call_deepseek(messages)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI 메시지 생성에 실패했습니다. 잠시 후 다시 시도해주세요.",
        )

    return GenerateMessageResponse(content=reply.strip())


@router.post("/analyze-delivery", response_model=AnalyzeDeliveryResponse)
async def api_analyze_delivery(
    payload: AnalyzeDeliveryRequest,
) -> AnalyzeDeliveryResponse:
    """Analyze delivery analytics data and return a natural-language report
    plus detected anomalies.

    The analytics data is sent as JSON text strings so the LLM can reason
    about it without us having to pre-parse every field.
    """
    data_lines = [f"[요약] {payload.summary}"]
    if payload.failures:
        data_lines.append(f"[실패 분석] {payload.failures}")
    if payload.accounts:
        data_lines.append(f"[계정 성과] {payload.accounts}")
    data_lines.append(f"[분석 기간] 최근 {payload.days}일")

    report, anomalies = await analyze_text_report(DELIVERY_SYSTEM_PROMPT, "\n".join(data_lines))
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI 분석 생성에 실패했습니다. 잠시 후 다시 시도해주세요.",
        )

    return AnalyzeDeliveryResponse(report=report, anomalies=anomalies)


@router.post("/suggest-reply", response_model=SuggestReplyResponse)
async def api_suggest_reply(payload: SuggestReplyRequest) -> SuggestReplyResponse:
    """Draft a reply suggestion for an arbitrary incoming customer message.

    Suggestion-only: this never sends anything — the operator reviews and
    sends the returned text manually through the existing send flow.
    """
    reply = await generate_reply_suggestion(payload.incoming_message)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI 답장 생성에 실패했습니다. 잠시 후 다시 시도해주세요.",
        )
    return SuggestReplyResponse(reply=reply)


@router.post("/generate-broadcast", response_model=GenerateBroadcastResponse)
async def api_generate_broadcast(payload: GenerateBroadcastRequest) -> GenerateBroadcastResponse:
    """Draft a broadcast message and, if candidate recipients were given,
    recommend which of *those exact* candidates fit the request.

    The model is restricted to choosing from ``candidate_recipients`` only
    (never asked to invent chat ids) so this stays grounded in real accessible
    groups/leads. Never calls create_broadcast itself — the frontend takes
    `message` + `recommended_chat_ids` into the existing broadcast flow.
    """
    candidates_text = (
        "\n".join(f"- {c.chat_id}: {c.name}" for c in payload.candidate_recipients)
        if payload.candidate_recipients
        else "(제공된 후보 없음)"
    )

    system_prompt = (
        "너는 TeleMon 서비스의 발송(Broadcast) 도우미야. "
        "사용자의 요청에 맞는 발송 메시지를 작성하고, 아래 '후보 대상 목록'에 있는 chat_id 중에서만 "
        "적합한 대상을 추천해줘. 목록에 없는 chat_id는 절대 만들어내지 마.\n\n"
        "반드시 아래 JSON 형식으로만 응답 (다른 텍스트 없이):\n"
        '{"message": "발송 메시지", "recommended_chat_ids": ["id1", "id2"], "reasoning": "선정 이유 한 줄"}\n\n"'
        f"[후보 대상 목록]\n{candidates_text}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": payload.prompt},
    ]

    reply = await _call_deepseek(messages)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI 발송 생성에 실패했습니다. 잠시 후 다시 시도해주세요.",
        )

    candidate_ids = {c.chat_id for c in payload.candidate_recipients}
    try:
        parsed = json.loads(reply.strip())
        message = str(parsed["message"]).strip()
        recommended = [cid for cid in parsed.get("recommended_chat_ids", []) if cid in candidate_ids]
        reasoning = str(parsed.get("reasoning", ""))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        # Model didn't return valid/expected JSON — degrade to just the raw
        # text as the message draft, with no recipient recommendation.
        message = reply.strip()
        recommended = []
        reasoning = ""

    return GenerateBroadcastResponse(message=message, recommended_chat_ids=recommended, reasoning=reasoning)


@router.post("/analyze-customers", response_model=AnalyzeCustomersResponse)
async def api_analyze_customers(payload: AnalyzeCustomersRequest) -> AnalyzeCustomersResponse:
    """Analyze customer/lead data and return a natural-language report plus
    notable insights (segment opportunities, churn risk, engagement drops)."""
    system_prompt = (
        "너는 TeleMon 서비스의 AI 고객 분석가야. "
        "고객/리드 데이터를 보고 운영자에게 의미 있는 인사이트를 제공해줘.\n\n"
        "응답 형식:\n"
        "1. 먼저 전반적인 고객 현황을 3-4문장으로 요약\n"
        "2. 주목할 만한 인사이트(insights)를 \";\"로 구분해서 나열 (예: 이탈 위험, 세그먼트 기회, 참여도 변화)\n"
        "3. 각 인사이트에 대해 이유와 제안 액션을 간단히 제시\n\n"
        "주의:\n"
        "- 한국어로 답변\n"
        "- 구체적인 수치를 포함\n"
        "- 운영자가 바로 조치할 수 있는 액션 아이템 위주로"
    )
    data_lines = [f"[고객 요약] {payload.summary}"]
    if payload.segments:
        data_lines.append(f"[세그먼트] {payload.segments}")
    data_lines.append(f"[분석 기간] 최근 {payload.days}일")

    report, insights = await analyze_text_report(system_prompt, "\n".join(data_lines))
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI 고객 분석에 실패했습니다. 잠시 후 다시 시도해주세요.",
        )

    return AnalyzeCustomersResponse(report=report, insights=insights)


@router.get("/ops-reports", response_model=list[AiOpsReportRead], dependencies=[Depends(require_admin)])
async def api_list_ops_reports(limit: int = 20, db: AsyncSession = Depends(get_db)):
    """List recent periodic AI ops reports (see app.services.ai_ops_service).

    Admin-only (unlike the rest of this router) — these reports aggregate
    cross-tenant data server-side, unlike generate-message/analyze-delivery
    which only ever echo back data the caller already supplied. Read-only —
    reports never trigger any action on their own.
    """
    reports = await list_recent_reports(db, limit=limit)
    return [
        AiOpsReportRead(
            id=r.id,
            report=r.report,
            anomalies=json.loads(r.anomalies_json or "[]"),
            created_at=r.created_at.isoformat(),
        )
        for r in reports
    ]
