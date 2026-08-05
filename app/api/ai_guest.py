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

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.core.rate_limiter import check_rate_limit, get_client_ip, get_retry_after_seconds
from app.database import get_db
from app.models.guest_ai_chat import GuestAiChatLog
from app.services.ai_chat_service import _MAX_TOKENS, _call_deepseek_full

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
    "당신은 TeleMon의 AI 어시스턴트입니다. 친절하고 간결하게 한국어로 답변하세요. "
    "지금 대화 상대는 아직 회원가입하지 않은 방문자이므로, 텔레그램 계정 연결이나 "
    "발송/자동응답 같은 TeleMon의 텔레그램 자동화 기능은 언급하지 마세요. "
    "만약 사용 중인 모델, 요금, 대화 횟수 제한에 대해 물어보면 반드시 다음 사실만 "
    "정확히 답하고, 절대로 지어내지 마세요: 당신은 TeleMon 전용으로 파인튜닝/설정된 "
    "자체 AI 모델이며, 특정 외부 회사의 제품 이름을 밝히지 마세요. "
    "로그인하지 않은 방문자는 IP당 하루 10회까지만 무료로 대화할 수 있고, "
    "10회를 초과하면 회원가입이 필요합니다. 이 제한을 없다거나 무제한이라고 답하지 "
    "마세요."
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
    # Populated only when think_mode was on and the model actually returned a
    # separate reasoning pass -- lets the frontend show "생각 중..." content.
    reasoning: str | None = None


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

    result = await _call_deepseek_full(messages, max_tokens=_MAX_TOKENS * 3 if payload.think_mode else _MAX_TOKENS)
    if result is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="AI 응답을 받아오지 못했습니다. 잠시 후 다시 시도해주세요.")
    reply, reasoning = result
    reasoning = reasoning if payload.think_mode else None

    logger.info("guest_ai_chat_reply", ip=client_ip)

    # Best-effort persistence for admin visibility -- must never fail the
    # actual chat response (the guest already got their reply above).
    try:
        db.add(GuestAiChatLog(ip=client_ip, message=payload.message, reply=reply))
        await db.commit()
    except Exception:
        logger.exception("guest_ai_chat_log_persist_failed")
        await db.rollback()

    return GuestChatResponse(reply=reply, reasoning=reasoning)
