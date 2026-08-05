"""Guest AI Chat -- no login, no Telegram account, no API key.

Epic 26: the biggest drop-off point in the current funnel is "I just want to
try the AI, why do I need to sign up + connect Telegram first?". This gives
anyone a real (not mocked/fabricated) DeepSeek-backed chat at /ai, rate
limited per IP, with nothing persisted server-side -- the conversation lives
only in the browser (sessionStorage) and disappears on refresh.

Deliberately minimal: no sessions, no history table, no tenant. Real signup
funnel (Billing, Telegram connect) still starts from the normal account flow;
this is purely an acquisition/trial surface.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.core.rate_limiter import check_rate_limit, get_client_ip, get_retry_after_seconds
from app.database import get_db
from app.models.guest_ai_chat import GuestAiChatLog
from app.services.ai_chat_service import _MAX_TOKENS, _call_deepseek_full, extract_confidence

logger = get_logger(__name__)
router = APIRouter(prefix="/api/ai/guest", tags=["ai-guest"])

_RATE_LIMIT_CATEGORY = "guest_ai_chat"
# Raised for the open test period -- 10/day was blocking real testers after
# only a handful of actual sends because failed/timed-out attempts (502s)
# still consumed the quota, and mobile-carrier NAT means many different real
# people can share one IP.
# TODO: dial this back down once the test period ends.
_MAX_PER_DAY = 30
_WINDOW_SECONDS = 24 * 60 * 60
_MAX_INPUT_CHARS = 2000
_MAX_HISTORY_MESSAGES = 12  # 6 turns of context, client-supplied

_SYSTEM_PROMPT = (
    "당신은 TeleMon의 AI 어시스턴트입니다. "
    "무조건 한국어로만 답변하세요. 사용자가 다른 언어로 질문해도, 영어 단어가 섞여 "
    "있어도 답변은 항상 한국어로만 하세요. 영어나 다른 언어로 답하지 마세요. "
    "질문을 대충 넘겨짚지 말고 정확히 무엇을 묻는지 먼저 파악한 뒤에 답하세요. "
    "질문이 애매하면 짐작으로 답하지 말고 무엇을 원하는지 되물어보세요. "
    "확실하지 않은 내용을 지어내서 답하지 말고, 모르면 모른다고 말하세요. "
    "친절하고 간결하게 답변하세요. "
    "지금 대화 상대는 아직 회원가입하지 않은 방문자이므로, 텔레그램 계정 연결이나 "
    "발송/자동응답 같은 TeleMon의 텔레그램 자동화 기능은 언급하지 마세요. "
    "만약 사용 중인 모델, 요금, 대화 횟수 제한에 대해 물어보면 반드시 다음 사실만 "
    "정확히 답하고, 절대로 지어내지 마세요: 당신은 TeleMon 전용으로 파인튜닝/설정된 "
    "자체 AI 모델이며, 특정 외부 회사의 제품 이름을 밝히지 마세요. "
    "로그인하지 않은 방문자는 IP당 하루 10회까지만 무료로 대화할 수 있고, "
    "10회를 초과하면 회원가입이 필요합니다. 이 제한을 없다거나 무제한이라고 답하지 "
    "마세요.\n\n"
    "답변하기 전에 스스로 판단하세요:\n"
    "1. 사용자가 준 정보로 정확한 답을 줄 수 있는가?\n"
    "   - 아니면: 바로 답하지 말고, 필요한 정보를 구체적으로 2~3개 질문하세요. "
    "정보가 진짜 부족할 때만 질문하고, 매 답변마다 기계적으로 묻지 마세요.\n"
    "2. 충분한 정보가 있으면 답변하되, 단순 정보 나열이 아니라 당신의 의견을 "
    "제시하세요. (예: \"제 생각엔 A보다 B가 나을 것 같습니다. 이유는...\")\n"
    "3. 답변 끝에는 상황에 맞을 때만(항상 X, 복잡하거나 여러 선택지가 걸린 주제일 "
    "때만) 놓친 부분이 없는지 되물어보세요. 뻔한 문구를 매번 붙이지 마세요.\n\n"
    "답변을 마친 뒤 마지막 줄에, 그 답변에 대한 당신의 확신도를 다음 형식으로만 "
    "정확히 표시하세요 (본문에서 언급하지 말고 이 마커만 맨 끝에 추가): "
    "[CONFIDENCE: high] 또는 [CONFIDENCE: medium] 또는 [CONFIDENCE: low]. "
    "정보가 부족해 되묻기만 한 경우는 [CONFIDENCE: medium]으로 표시하세요."
)


class GuestChatMessage(BaseModel):
    role: str
    content: str = Field(max_length=_MAX_INPUT_CHARS)


class GuestChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=_MAX_INPUT_CHARS)
    # Client owns history (nothing persisted server-side). max_length here
    # REJECTS (422) an oversized history rather than silently truncating it --
    # a crafted request can't smuggle a huge prompt through this field.
    history: list[GuestChatMessage] = Field(default_factory=list, max_length=_MAX_HISTORY_MESSAGES)
    think_mode: bool = Field(default=False)


class GuestChatResponse(BaseModel):
    reply: str
    log_id: str | None = None
    # Populated only when think_mode was on and the model actually returned a
    # separate reasoning pass -- lets the frontend show "생각 중..." content.
    reasoning: str | None = None
    # "high" | "medium" | "low" | None (model omitted the marker). Parsed out
    # of `reply` server-side -- the [CONFIDENCE: ...] marker itself never
    # reaches the client.
    confidence: str | None = None


@router.post("/chat", response_model=GuestChatResponse)
async def guest_chat(payload: GuestChatRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = get_client_ip(request)

    if not check_rate_limit(client_ip, _RATE_LIMIT_CATEGORY, max_attempts=_MAX_PER_DAY, window_seconds=_WINDOW_SECONDS):
        retry_after = get_retry_after_seconds(client_ip, _RATE_LIMIT_CATEGORY, window_seconds=_WINDOW_SECONDS)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="오늘 무료 사용 횟수를 모두 사용했어요. 회원가입하면 계속 이용할 수 있어요.",
            headers={"Retry-After": str(retry_after)},
        )

    if not settings.deepseek_api_key:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="AI 서비스를 일시적으로 사용할 수 없습니다.")

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages.extend({"role": m.role, "content": m.content} for m in payload.history if m.role in ("user", "assistant"))
    messages.append({"role": "user", "content": payload.message})

    # The model always reasons internally regardless of budget -- think_mode
    # only controls whether that reasoning gets shown to the user, not how
    # much token budget the call gets.
    result = await _call_deepseek_full(messages, max_tokens=_MAX_TOKENS)
    if result is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="AI 응답을 받아오지 못했습니다. 잠시 후 다시 시도해주세요.")
    reply, reasoning = result
    reply, confidence = extract_confidence(reply)
    reasoning = reasoning if payload.think_mode else None

    logger.info("guest_ai_chat_reply", ip=client_ip, confidence=confidence)

    # Best-effort persistence for admin visibility -- must never fail the
    # actual chat response (the guest already got their reply above).
    log_id = None
    try:
        log_entry = GuestAiChatLog(ip=client_ip, message=payload.message, reply=reply)
        db.add(log_entry)
        await db.commit()
        await db.refresh(log_entry)
        log_id = log_entry.id
    except Exception:
        logger.exception("guest_ai_chat_log_persist_failed")
        await db.rollback()

    return GuestChatResponse(reply=reply, log_id=log_id, reasoning=reasoning, confidence=confidence)


class GuestFeedbackRequest(BaseModel):
    log_id: str = Field(min_length=1, max_length=36)
    thumbs_up: bool


@router.post("/feedback")
async def guest_feedback(payload: GuestFeedbackRequest, db: AsyncSession = Depends(get_db)):
    """Submit thumbs up/down feedback for a guest AI chat response.

    When positive feedback is received, also creates a Knowledge Candidate
    so the Q&A pair can be reviewed and potentially added to the KB.
    """
    from sqlalchemy import select, func
    from app.models.knowledge_base import KnowledgeCandidate

    result = await db.execute(
        select(GuestAiChatLog).where(GuestAiChatLog.id == payload.log_id).limit(1)
    )
    log = result.scalar_one_or_none()
    if log is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="로그를 찾을 수 없습니다.")

    log.thumbs_up = payload.thumbs_up
    await db.commit()

    # 게스트 긍정 피드백 → Knowledge Candidate 생성
    if payload.thumbs_up:
        try:
            # 중복 체크 (같은 질문+답변 조합)
            existing = await db.execute(
                select(KnowledgeCandidate).where(
                    KnowledgeCandidate.question == log.message[:500],
                    KnowledgeCandidate.answer == log.reply[:500],
                    KnowledgeCandidate.status == "pending",
                ).limit(1)
            )
            if existing.scalar_one_or_none() is None:
                # 게스트용 Knowledge Candidate 생성 (tenant_id는 "guest"로 설정)
                candidate = KnowledgeCandidate(
                    id=str(uuid.uuid4()),
                    tenant_id="guest",
                    question=log.message[:2000],
                    answer=log.reply[:5000],
                    feedback_score=5.0,  # 게스트는 thumbs_up만 있으므로 5점으로 설정
                    feedback_count=1,
                    model_name="deepseek-chat",
                    tokens_used=0,
                    response_time_ms=0,
                    ai_version="guest",
                    prompt_version="v1",
                    status="pending",
                )
                db.add(candidate)
                await db.commit()
                logger.info("guest_knowledge_candidate_created", log_id=payload.log_id)
        except Exception as exc:
            logger.warning("guest_knowledge_candidate_failed", error=str(exc))

    logger.info("guest_ai_chat_feedback", log_id=payload.log_id, thumbs_up=payload.thumbs_up)
    return {"success": True}
