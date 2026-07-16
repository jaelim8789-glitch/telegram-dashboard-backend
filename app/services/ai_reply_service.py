"""AI-drafted reply suggestions — "AI Reply" (standalone) and the Auto Reply
AI fallback (per-account opt-in, see Account.ai_fallback_reply_enabled).

Suggestion-only by design: nothing in this module ever sends a Telegram
message. It only drafts text (via the shared DeepSeek call in
app.services.ai_chat_service) and, for the auto-reply fallback path,
persists it as an AutoReplySuggestion for an operator to review and send
manually. No new LLM provider — reuses _call_deepseek exactly like
app.api.ai_assist and app.services.ai_chat_service.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.auto_reply import AutoReplySuggestion
from app.services.ai_chat_service import _call_deepseek

logger = get_logger(__name__)

_SYSTEM_PROMPT = (
    "너는 TeleMon 서비스의 답장 작성 도우미야. "
    "고객이 보낸 메시지에 대해 자연스럽고 도움이 되는 답장 초안을 작성해줘.\n\n"
    "규칙:\n"
    "- 결과는 반드시 한국어로 출력\n"
    "- 답장 내용만 출력 (설명/코멘트 없이)\n"
    "- 2000자 이내로 작성\n"
    "- 톤은 친근하고 전문적으로\n"
    "- 이 답장은 사람이 검토 후 직접 전송하므로, 확신이 없으면 그렇게 드러내도 좋음"
)


async def generate_reply_suggestion(incoming_message: str) -> str | None:
    """Draft a reply to `incoming_message`. Returns None on any DeepSeek failure
    (not-configured, timeout, HTTP error) — callers should treat None as
    "no suggestion available" rather than raise."""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": incoming_message},
    ]
    reply = await _call_deepseek(messages)
    return reply.strip() if reply else None


async def record_auto_reply_suggestion(
    db: AsyncSession,
    *,
    account_id: str,
    chat_id: str,
    user_id: str,
    user_name: str | None,
    trigger_message: str,
) -> AutoReplySuggestion | None:
    """Draft and persist a suggestion for a message that matched no AutoReplyRule.
    Returns None (and persists nothing) if DeepSeek couldn't produce a reply —
    this must never raise into the Telethon event handler that calls it."""
    suggested_reply = await generate_reply_suggestion(trigger_message)
    if suggested_reply is None:
        return None

    suggestion = AutoReplySuggestion(
        account_id=account_id,
        chat_id=chat_id,
        user_id=user_id,
        user_name=user_name,
        trigger_message=trigger_message,
        suggested_reply=suggested_reply,
    )
    db.add(suggestion)
    await db.commit()
    await db.refresh(suggestion)
    logger.info("auto_reply_suggestion_recorded", account_id=account_id, chat_id=chat_id)
    return suggestion
