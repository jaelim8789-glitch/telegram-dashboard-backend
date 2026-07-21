from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ai_tools import TOOL_META, TOOLS, execute_tool
from app.api.deps import Identity
from app.core.logging import get_logger
from app.core.telegram_identity import tg_identifier
from app.models.tenant import AiChatMessage, Tenant
from app.services.ai_core_service import call_deepseek
from app.services.usage_tracker import get_monthly_usage, record_usage

logger = get_logger(__name__)

_MAX_INPUT_CHARS = 2000
_MAX_HISTORY_MESSAGES = 24
_MSG_USAGE_COST = 1
_WRITE_TOOL_USAGE_COST = 3
_PENDING_TTL_MINUTES = 10

_in_flight: dict[int, bool] = {}
_pending_actions: dict[int, "PendingAction"] = {}
_write_tool_last_used: dict[str, float] = {}
_WRITE_TOOL_COOLDOWN_SECONDS = 30


@dataclass
class PendingAction:
    request_id: str
    tenant_id: str
    tool_name: str
    label: str
    arguments: dict
    expires_at: datetime


@dataclass
class BotAiAgentResult:
    status: str
    reply: str | None = None
    detail: str = ""
    pending: PendingAction | None = None


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_in_flight(telegram_user_id: int) -> bool:
    return _in_flight.get(telegram_user_id, False)


def _set_in_flight(telegram_user_id: int, value: bool) -> None:
    if value:
        _in_flight[telegram_user_id] = True
    else:
        _in_flight.pop(telegram_user_id, None)


def clear_pending_action(telegram_user_id: int) -> None:
    _pending_actions.pop(telegram_user_id, None)


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
    result = await db.execute(
        select(AiChatMessage)
        .where(
            AiChatMessage.tenant_id == tenant_id,
            AiChatMessage.telegram_user_id == telegram_user_id,
        )
        .order_by(AiChatMessage.created_at.desc())
        .limit(_MAX_HISTORY_MESSAGES)
    )
    rows = list(reversed(result.scalars().all()))
    return [{"role": row.role, "content": row.content} for row in rows]


def _consume_ai_quota_or_credit(tenant: Tenant, used: int, cost: int) -> tuple[bool, int]:
    limit = max(int(tenant.monthly_ai_chat_limit or 0), 0)
    if limit <= 0:
        needed_credit = cost
    else:
        free_remaining = max(limit - used, 0)
        needed_credit = max(cost - free_remaining, 0)

    if needed_credit > (tenant.ai_chat_credit_balance or 0):
        return False, needed_credit
    return True, needed_credit


def _apply_credit_deduction(tenant: Tenant, credit_cost: int) -> None:
    if credit_cost <= 0:
        return
    tenant.ai_chat_credit_balance = max((tenant.ai_chat_credit_balance or 0) - credit_cost, 0)


def _check_write_tool_rate_limit(tenant_id: str) -> tuple[bool, int]:
    now = datetime.now(timezone.utc).timestamp()
    last = _write_tool_last_used.get(tenant_id, 0.0)
    elapsed = now - last
    if elapsed < _WRITE_TOOL_COOLDOWN_SECONDS:
        return False, int(ceil(_WRITE_TOOL_COOLDOWN_SECONDS - elapsed))
    _write_tool_last_used[tenant_id] = now
    return True, 0


def _build_system_prompt() -> str:
    return (
        "당신은 TeleMon 운영 AI 에이전트입니다. 한국어로 간결하고 실행 가능한 답변을 합니다. "
        "도구 호출이 가능한 경우에는 반드시 도구를 사용해 실제 데이터를 조회하세요. "
        "메시지 발송 요청은 send_broadcast 도구를 사용하되, 실행은 사용자 확인 이후에만 가능합니다. "
        "도구 실행 결과를 근거로 숫자와 상태를 명확히 요약하세요."
    )


async def _execute_send_broadcast(arguments: dict) -> dict:
    account_id = arguments.get("account_id")
    recipients = arguments.get("recipients") or arguments.get("group_ids")
    message = arguments.get("message")

    missing = [
        k
        for k, v in (("account_id", account_id), ("recipients/group_ids", recipients), ("message", message))
        if not v
    ]
    if missing:
        raise ValueError(f"발송 Tool에 필수 정보가 없습니다: {', '.join(missing)}")
    if not isinstance(recipients, list) or len(recipients) == 0:
        raise ValueError("발송 대상(recipients/group_ids)은 비어있지 않은 배열이어야 합니다.")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message는 비어있지 않은 문자열이어야 합니다.")

    from app.services.delivery import DeliveryRequest, deliver_message

    request = DeliveryRequest(
        account_id=str(account_id),
        recipients=[str(r) for r in recipients],
        message=message,
        media_path=arguments.get("media_path"),
        source="ai_agent_tool",
        source_id=str(account_id),
        reply_to_msg_id=arguments.get("reply_to_msg_id"),
        inline_buttons=arguments.get("inline_buttons"),
    )
    results = await deliver_message(request)
    succeeded = [r.recipient for r in results if r.status.value == "success"]
    failed = [{"recipient": r.recipient, "error": r.error_message} for r in results if r.status.value != "success"]
    return {
        "tool": "send_broadcast",
        "delivered": len(succeeded),
        "failed": len(failed),
        "succeeded_recipients": succeeded,
        "errors": failed,
    }


def _build_confirmation_text(tool_name: str, arguments: dict, label: str) -> str:
    message = str(arguments.get("message") or "")
    preview = (message[:180] + "...") if len(message) > 180 else message
    recipients = arguments.get("recipients") or arguments.get("group_ids") or []
    target_count = len(recipients) if isinstance(recipients, list) else 0
    account_id = arguments.get("account_id") or "-"
    return (
        f"⚠️ 실행 확인 필요\n"
        f"도구: {label} ({tool_name})\n"
        f"계정: {account_id}\n"
        f"대상 수: {target_count}\n"
        f"메시지: {preview or '-'}\n\n"
        "아래 버튼으로 실행 여부를 선택해주세요."
    )


async def send_message(db: AsyncSession, telegram_user_id: int, text: str) -> BotAiAgentResult:
    if _is_in_flight(telegram_user_id):
        return BotAiAgentResult(status="rate_limited", detail="이전 요청을 처리 중입니다. 잠시 후 다시 시도해주세요.")

    message_text = (text or "").strip()
    if len(message_text) > _MAX_INPUT_CHARS:
        return BotAiAgentResult(
            status="too_long",
            detail=f"메시지가 너무 깁니다 ({_MAX_INPUT_CHARS}자 이하로 입력해주세요).",
        )
    if not message_text:
        return BotAiAgentResult(status="too_long", detail="메시지를 입력해주세요.")

    _set_in_flight(telegram_user_id, True)
    try:
        return await _do_send(db, telegram_user_id, message_text)
    finally:
        _set_in_flight(telegram_user_id, False)


async def _do_send(db: AsyncSession, telegram_user_id: int, text: str) -> BotAiAgentResult:
    tenant = await _find_tenant(db, telegram_user_id)
    if tenant is None:
        return BotAiAgentResult(
            status="not_linked",
            detail="연결된 TeleMon 계정을 찾을 수 없습니다. 먼저 웹사이트에서 가입/연동을 완료해주세요.",
        )

    if not (_is_subscription_active(tenant) or _is_trial_valid(tenant)):
        return BotAiAgentResult(
            status="not_eligible",
            detail="현재 유효한 요금제 또는 무료체험이 없습니다. 결제를 완료하거나 무료체험을 시작해주세요.",
        )

    used = await get_monthly_usage(db, tenant.id, action="ai_chat")
    allowed, msg_credit_cost = _consume_ai_quota_or_credit(tenant, used, _MSG_USAGE_COST)
    if not allowed:
        return BotAiAgentResult(
            status="quota_exceeded",
            detail=(
                f"AI Chat 한도를 초과했습니다. 월 기본 한도 {tenant.monthly_ai_chat_limit}회 사용 완료 후 "
                "추가 크레딧도 부족합니다."
            ),
        )

    identity = Identity(kind="user", tenant_id=tenant.id)
    history = await _recent_history(db, tenant.id, str(telegram_user_id))
    messages = [{"role": "system", "content": _build_system_prompt()}, *history, {"role": "user", "content": text}]

    answer, _, tool_calls = await call_deepseek(messages, max_tokens=1500, tools=TOOLS)

    pending_action: PendingAction | None = None
    tool_failures: list[str] = []

    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            try:
                arguments = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                arguments = {}

            meta = TOOL_META.get(tool_name, {})
            if meta.get("requires_confirmation", False):
                request_id = str(uuid.uuid4())
                pending_action = PendingAction(
                    request_id=request_id,
                    tenant_id=tenant.id,
                    tool_name=tool_name,
                    label=meta.get("label", tool_name),
                    arguments=arguments,
                    expires_at=_utcnow_naive() + timedelta(minutes=_PENDING_TTL_MINUTES),
                )
                _pending_actions[telegram_user_id] = pending_action
                if not answer:
                    answer = _build_confirmation_text(tool_name, arguments, pending_action.label)
                break

            tr = await execute_tool(tool_name, arguments, identity)
            if tr.success:
                messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": json.dumps(tr.result, ensure_ascii=False, default=str),
                    }
                )
            else:
                tool_failures.append(f"{tool_name}: {tr.error or '실행 실패'}")

        if not pending_action and (len(messages) > len(history) + 2 or tool_failures):
            follow_up, _, _ = await call_deepseek(messages, max_tokens=1200)
            if follow_up:
                answer = follow_up
            elif tool_failures and not answer:
                answer = "일부 도구 실행에 실패했습니다. " + "; ".join(tool_failures[:3])

    if not answer:
        answer = "죄송합니다. 응답 생성에 실패했습니다. 잠시 후 다시 시도해주세요."

    db.add(AiChatMessage(tenant_id=tenant.id, telegram_user_id=str(telegram_user_id), role="user", content=text))
    db.add(AiChatMessage(tenant_id=tenant.id, telegram_user_id=str(telegram_user_id), role="assistant", content=answer))

    await record_usage(tenant.id, "ai_chat", _MSG_USAGE_COST)
    _apply_credit_deduction(tenant, msg_credit_cost)
    await db.commit()

    if pending_action is not None:
        return BotAiAgentResult(status="pending_confirmation", reply=answer, pending=pending_action)
    return BotAiAgentResult(status="ok", reply=answer)


async def confirm_pending_action(
    db: AsyncSession,
    telegram_user_id: int,
    request_id: str,
    approved: bool,
) -> BotAiAgentResult:
    pending = _pending_actions.get(telegram_user_id)
    if pending is None:
        return BotAiAgentResult(status="no_pending", detail="확인 대기 중인 요청이 없습니다.")

    if pending.request_id != request_id:
        return BotAiAgentResult(status="expired", detail="요청 식별자가 일치하지 않습니다. 다시 요청해주세요.")

    if pending.expires_at < _utcnow_naive():
        _pending_actions.pop(telegram_user_id, None)
        return BotAiAgentResult(status="expired", detail="확인 가능한 시간이 만료되었습니다. 다시 요청해주세요.")

    if not approved:
        _pending_actions.pop(telegram_user_id, None)
        return BotAiAgentResult(status="cancelled", detail="요청을 취소했습니다.")

    tenant = await _find_tenant(db, telegram_user_id)
    if tenant is None or tenant.id != pending.tenant_id:
        _pending_actions.pop(telegram_user_id, None)
        return BotAiAgentResult(status="not_linked", detail="연결된 계정을 확인할 수 없습니다.")

    ok, remain = _check_write_tool_rate_limit(tenant.id)
    if not ok:
        return BotAiAgentResult(status="rate_limited", detail=f"발송 도구는 30초에 한 번만 실행할 수 있습니다. {remain}초 후 시도해주세요.")

    used = await get_monthly_usage(db, tenant.id, action="ai_chat")
    allowed, tool_credit_cost = _consume_ai_quota_or_credit(tenant, used, _WRITE_TOOL_USAGE_COST)
    if not allowed:
        return BotAiAgentResult(
            status="quota_exceeded",
            detail=(
                f"AI Chat 한도를 초과했습니다. 이번 실행에는 추가 크레딧 {tool_credit_cost}회가 필요하지만 잔액이 부족합니다."
            ),
        )

    try:
        if pending.tool_name == "send_broadcast":
            result = await _execute_send_broadcast(pending.arguments)
        else:
            return BotAiAgentResult(status="server_error", detail=f"지원하지 않는 도구입니다: {pending.tool_name}")
    except ValueError as exc:
        return BotAiAgentResult(status="server_error", detail=f"실행 정보가 올바르지 않습니다: {str(exc)}")
    except Exception as exc:
        logger.error("bot_ai_agent_confirm_failed", tool=pending.tool_name, error=str(exc))
        return BotAiAgentResult(status="server_error", detail=f"실행 중 오류가 발생했습니다: {str(exc)}")

    _pending_actions.pop(telegram_user_id, None)

    await record_usage(tenant.id, "ai_chat", _WRITE_TOOL_USAGE_COST)
    _apply_credit_deduction(tenant, tool_credit_cost)

    summary = (
        f"✅ 실행 완료\n"
        f"도구: {pending.label}\n"
        f"성공: {result.get('delivered', 0)}\n"
        f"실패: {result.get('failed', 0)}"
    )
    db.add(AiChatMessage(tenant_id=tenant.id, telegram_user_id=str(telegram_user_id), role="assistant", content=summary))
    await db.commit()

    return BotAiAgentResult(status="executed", reply=summary)