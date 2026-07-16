"""Bot-facing "🤖 AI Chat" — a general-purpose DeepSeek-backed assistant reachable
from the Telegram ops menu. Mirrors how app.services.bot_api_key_service and
app.services.bot_account_service are organized: all DB/business/HTTP logic lives
here, telegram_bot_service.py only formats replies and wires callbacks/handlers.

Eligibility reuses the exact same tenant-lookup and trial/subscription-validity
rules as bot_api_key_service.py (a linked, currently-eligible TeleMon account) —
duplicated here rather than imported, matching that module's own precedent of
not sharing this logic across bot services.

Quota model: each tenant gets `monthly_ai_chat_limit` free replies per calendar
month (tracked via the existing usage_tracker UsageRecord ledger, action=
"ai_chat"). Once that's exhausted, `tenant.ai_chat_credit_balance` (topped up via
the "ai_chat_pack_50" Telegram Stars add-on, see app/services/billing.py) is
spent one credit per reply. A failed/timed-out DeepSeek call consumes neither.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.core.telegram_identity import tg_identifier
from app.models.tenant import AiChatMessage, Tenant
from app.services.usage_tracker import get_monthly_usage, record_usage

logger = get_logger(__name__)

# Reject oversized input before spending any quota or making an API call.
_MAX_INPUT_CHARS = 2000
_MAX_TOKENS = 600


@dataclass
class AiChatResult:
    """Outcome of a single "AI Chat" turn.

    ``status`` is one of:
      * ``"ok"``              — success, ``reply`` holds the assistant's text
      * ``"not_linked"``      — Telegram account not linked to any TeleMon tenant
      * ``"not_eligible"``    — linked but no valid subscription/trial
      * ``"quota_exceeded"``  — monthly quota used up and no Stars credit left
      * ``"rate_limited"``    — a previous request from this user is still in flight
      * ``"too_long"``        — input exceeds _MAX_INPUT_CHARS
      * ``"server_error"``    — DeepSeek not configured, or the API call failed
    """

    status: str
    reply: str | None = None
    detail: str = ""


# ─── Per-user in-flight lock (mirrors bot_api_key_service._in_flight) ──

_in_flight: dict[int, bool] = {}


def _is_in_flight(telegram_user_id: int) -> bool:
    return _in_flight.get(telegram_user_id, False)


def _set_in_flight(telegram_user_id: int, value: bool) -> None:
    if value:
        _in_flight[telegram_user_id] = True
    else:
        _in_flight.pop(telegram_user_id, None)


# ─── Helpers ───────────────────────────────────────────────────────────


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_trial_valid(tenant: Tenant) -> bool:
    if tenant.subscription_status == "active":
        return True
    if tenant.trial_expires_at is not None and tenant.trial_expires_at > _utcnow_naive():
        return True
    return False


def _is_subscription_active(tenant: Tenant) -> bool:
    if tenant.subscription_status != "active":
        return False
    if tenant.billing_period_end is not None and tenant.billing_period_end < _utcnow_naive():
        return False
    return True


async def _find_tenant(db: AsyncSession, telegram_user_id: int) -> Tenant | None:
    identifier = tg_identifier(telegram_user_id)
    result = await db.execute(select(Tenant).where(Tenant.phone == identifier).limit(1))
    return result.scalar_one_or_none()


async def _recent_history(db: AsyncSession, tenant_id: str, telegram_user_id: str) -> list[dict]:
    """Last ai_chat_history_turns*2 messages (user+assistant pairs), oldest first."""
    limit = max(settings.ai_chat_history_turns, 0) * 2
    if limit == 0:
        return []
    result = await db.execute(
        select(AiChatMessage)
        .where(
            AiChatMessage.tenant_id == tenant_id,
            AiChatMessage.telegram_user_id == telegram_user_id,
        )
        .order_by(AiChatMessage.created_at.desc())
        .limit(limit)
    )
    rows = list(reversed(result.scalars().all()))
    return [{"role": row.role, "content": row.content} for row in rows]


async def _call_deepseek(messages: list[dict]) -> str | None:
    """Returns the assistant's reply text, or None on any failure."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{settings.deepseek_api_base}/chat/completions",
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                json={
                    "model": settings.deepseek_model,
                    "messages": messages,
                    "max_tokens": _MAX_TOKENS,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        logger.error("ai_chat_deepseek_call_failed", error=str(exc))
        return None


# ─── Main entry point ─────────────────────────────────────────────────


async def send_message(db: AsyncSession, telegram_user_id: int, text: str) -> AiChatResult:
    if _is_in_flight(telegram_user_id):
        return AiChatResult(status="rate_limited", detail="이전 메시지를 처리 중입니다. 잠시 후 다시 시도해주세요.")

    if len(text) > _MAX_INPUT_CHARS:
        return AiChatResult(
            status="too_long",
            detail=f"메시지가 너무 깁니다 ({_MAX_INPUT_CHARS}자 이하로 입력해주세요).",
        )

    _set_in_flight(telegram_user_id, True)
    try:
        return await _do_send(db, telegram_user_id, text)
    finally:
        _set_in_flight(telegram_user_id, False)


async def _do_send(db: AsyncSession, telegram_user_id: int, text: str) -> AiChatResult:
    if not settings.deepseek_api_key:
        return AiChatResult(status="server_error", detail="AI Chat이 아직 설정되지 않았습니다. 잠시 후 다시 시도해주세요.")

    tenant = await _find_tenant(db, telegram_user_id)
    if tenant is None:
        return AiChatResult(
            status="not_linked",
            detail="연결된 TeleMon 계정을 찾을 수 없습니다.\n먼저 웹사이트에서 회원가입(무료 체험)을 완료해주세요.",
        )

    if not (_is_subscription_active(tenant) or _is_trial_valid(tenant)):
        return AiChatResult(
            status="not_eligible",
            detail="현재 유효한 요금제 또는 무료 체험이 없습니다.\n결제를 완료하거나 무료 체험을 시작해주세요.",
        )

    monthly_used = await get_monthly_usage(db, tenant.id, action="ai_chat")
    use_credit = monthly_used >= tenant.monthly_ai_chat_limit
    if use_credit and tenant.ai_chat_credit_balance <= 0:
        return AiChatResult(
            status="quota_exceeded",
            detail=(
                f"이번 달 AI Chat 사용량({tenant.monthly_ai_chat_limit}회)을 모두 소진했습니다.\n"
                "요금제를 업그레이드하거나 Stars로 추가 사용권을 구매해주세요."
            ),
        )

    telegram_user_id_str = str(telegram_user_id)
    history = await _recent_history(db, tenant.id, telegram_user_id_str)
    messages = [{"role": "system", "content": settings.ai_chat_system_prompt}, *history, {"role": "user", "content": text}]

    reply = await _call_deepseek(messages)
    if reply is None:
        return AiChatResult(status="server_error", detail="일시적인 오류로 응답을 받지 못했습니다. 잠시 후 다시 시도해주세요.")

    db.add(AiChatMessage(tenant_id=tenant.id, telegram_user_id=telegram_user_id_str, role="user", content=text))
    db.add(AiChatMessage(tenant_id=tenant.id, telegram_user_id=telegram_user_id_str, role="assistant", content=reply))
    await record_usage(tenant.id, "ai_chat")
    if use_credit:
        tenant.ai_chat_credit_balance -= 1
    await db.commit()

    logger.info("ai_chat_reply_sent", tenant_id=tenant.id, used_credit=use_credit)
    return AiChatResult(status="ok", reply=reply)
