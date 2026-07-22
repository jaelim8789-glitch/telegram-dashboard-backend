"""
Update-handling logic for the Telegram bot.

Bridges Telegram Bot API updates into the existing free-API-key
verification flow (backend/routers/free_api_key.py) *without modifying
that module* — its request DB helpers are imported and reused as-is,
so the request/response contract the frontend already depends on
(src/lib/api_free_api_key.ts) is untouched.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.admin_platform import AdminPlatform, AuditAction
from app.production_config import get_config
from app.api.free_api_key import router as free_api_key_router
from app.bot import db as bot_db
from app.bot.ai_employee import AiEmployee
from app.bot.guest_engine import GuestEngine
from app.bot.telegram_api import TelegramAPIError, TelegramBotClient, is_channel_member_status
from app.database import async_session_maker
from app.models.referral import ReferralCommission
from app.models.tenant import Tenant

logger = logging.getLogger(__name__)

_VERIFY_BUTTON_TEXT = "✅ 채널 가입 확인"

_GREETING = (
    "🌟 안녕하세요, **TeleMon**입니다!\n\n"
    "무엇을 도와드릴까요?\n"
    "궁금한 점이 있으면 자유롭게 물어봐 주세요 ✨\n"
    "명령어 목록은 `/menu` 를 입력하세요."
)

_MENU_COMMANDS = [
    [{"text": "🔑 내 API 키"}, {"text": "📋 내 플랜"}],
    [{"text": "💳 결제"}, {"text": "📜 구매내역"}],
    [{"text": "✅ 출석체크"}, {"text": "🤝 추천인"}],
    [{"text": "⭐ Stars"}, {"text": "🤖 자동응답"}],
    [{"text": "📢 공지사항"}],
]

_COMMAND_MAP: dict[str, str] = {
    "🔑 내 api 키": "api_key",
    "🔑 내 API 키": "api_key",
    "📋 내 플랜": "plan",
    "📋 내 plan": "plan",
    "💳 결제": "payment",
    "📜 구매내역": "purchase",
    "✅ 출석체크": "checkin",
    "🤝 추천인": "referral",
    "⭐ stars": "stars",
    "🤖 자동응답": "autoreply",
    "📢 공지사항": "notice",
    "/menu": "menu",
    "/start": "start",
}

_MENU_RESPONSES: dict[str, str] = {
    "api_key": "🔑 **내 API 키**\n\n웹사이트에서 API 키를 확인하세요: https://app.telemon.online",
    "plan": "📋 **내 플랜**\n\n웹사이트에서 플랜 정보를 확인하세요: https://app.telemon.online",
    "payment": "💳 **결제**\n\nStars 결제: `/buy` 명령어를 사용하거나 웹사이트를 방문하세요.",
    "purchase": "📜 **구매내역**\n\n웹사이트에서 구매내역을 확인하세요: https://app.telemon.online",
    "checkin": "✅ **출석체크**\n\n웹사이트에서 출석체크를 진행하세요: https://app.telemon.online",
    "referral": "🤝 **추천인**\n\n웹사이트에서 추천인 코드를 확인하세요: https://app.telemon.online",
    "stars": "⭐ **Stars**\n\n웹사이트에서 Stars 잔액을 확인하세요: https://app.telemon.online",
    "autoreply": "🤖 **자동응답**\n\n웹사이트에서 자동응답을 설정하세요: https://app.telemon.online",
    "notice": "📢 **공지사항**\n\n최신 소식은 공식 채널을 확인하세요: https://t.me/telemon_official",
    "menu": "📋 **명령어 목록**\n\n아래 버튼을 눌러 원하는 기능을 선택하세요.",
}


def _client() -> TelegramBotClient | None:
    cfg = get_config().telegram_bot
    if not cfg.bot_token:
        return None
    return TelegramBotClient(cfg.bot_token)


async def notify_admins(text: str, event_type: str = "info") -> None:
    cfg = get_config().telegram_bot
    client = _client()
    if not client or not cfg.admin_chat_ids:
        bot_db.log_admin_notify(event_type, text, delivered=False)
        return
    delivered = False
    for chat_id in cfg.admin_chat_ids:
        try:
            await client.send_message(chat_id, text)
            delivered = True
        except Exception as e:
            logger.warning("Admin notify failed for chat_id=%s: %s", chat_id, e)
    bot_db.log_admin_notify(event_type, text, delivered=delivered)


def _verify_keyboard(token: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": _VERIFY_BUTTON_TEXT, "callback_data": f"verify:{token}"}]]}


async def _handle_verify_start(client: TelegramBotClient, chat_id: int, message: dict[str, Any]) -> None:
    text = message.get("text", "")
    parts = text.split(maxsplit=1)
    token = parts[1].strip() if len(parts) > 1 else ""

    if not token:
        await client.send_message(chat_id, "잘못된 접근입니다. 웹사이트에서 다시 시도해주세요.")
        return

    req = free_api_key_router._get_request(token)
    if not req:
        await client.send_message(chat_id, "인증 토큰을 찾을 수 없습니다. 웹사이트에서 다시 시도해주세요.")
        return

    from_user = message.get("from", {})
    bot_db.upsert_session(
        chat_id=str(chat_id),
        token=token,
        telegram_user_id=from_user.get("id"),
        telegram_username=from_user.get("username"),
    )

    cfg = get_config().telegram_bot
    channel_note = f"\n\n📢 채널: {cfg.channel_id}" if cfg.channel_id else ""
    msg = (
        "🔗 **채널 인증이 필요합니다.**\n\n"
        "무료 체험 API 키를 발급받으려면 "
        "아래 채널에 가입한 후 인증 버튼을 눌러주세요."
        + channel_note
    )
    await client.send_message(chat_id, msg, reply_markup=_verify_keyboard(token))


async def _handle_verify_callback(client: TelegramBotClient, callback_query: dict[str, Any]) -> None:
    callback_id = callback_query["id"]
    data = callback_query.get("data", "")
    chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
    from_user = callback_query.get("from", {})
    user_id = from_user.get("id")

    token = data.split(":", 1)[1] if ":" in data else ""
    cfg = get_config().telegram_bot

    if not token or not cfg.channel_id or not user_id:
        await client.answer_callback_query(callback_id, "설정 오류입니다. 관리자에게 문의해주세요.", show_alert=True)
        return

    try:
        member = await client.get_chat_member(cfg.channel_id, user_id)
        status = member.get("status", "")
    except TelegramAPIError as e:
        logger.warning("getChatMember failed: %s", e)
        await client.answer_callback_query(
            callback_id, "채널 상태를 확인할 수 없습니다. 봇이 채널 관리자로 등록되어 있는지 확인해주세요.", show_alert=True,
        )
        return

    if not is_channel_member_status(status):
        free_api_key_router._upsert_request(token, status="unverified", reason="not_channel_member")
        await client.answer_callback_query(callback_id, "아직 채널에 가입하지 않은 것 같습니다.", show_alert=True)
        return

    free_api_key_router._upsert_request(token, status="verified", reason=None)
    await client.answer_callback_query(callback_id, "✅ 인증 완료!")
    if chat_id is not None:
        await client.send_message(chat_id, "✅ 채널 가입이 확인되었습니다. 웹사이트로 돌아가 API 키를 발급받으세요.")

    username = from_user.get("username") or str(user_id)
    await notify_admins(f"[TeleMon] 신규 채널 인증 완료: @{username} (token={token[:8]}...)", event_type="verified")


# ── AI-powered chat handler ─────────────────────────────────────────

_GUEST_ENGINE_INSTANCE: GuestEngine | None = None
_AI_EMPLOYEE_INSTANCE: AiEmployee | None = None


def _get_guest_engine(client: TelegramBotClient) -> GuestEngine | None:
    global _GUEST_ENGINE_INSTANCE
    if _GUEST_ENGINE_INSTANCE is None:
        _GUEST_ENGINE_INSTANCE = GuestEngine(client)
    return _GUEST_ENGINE_INSTANCE


def _get_ai_employee(client: TelegramBotClient) -> AiEmployee | None:
    global _AI_EMPLOYEE_INSTANCE
    if _AI_EMPLOYEE_INSTANCE is None:
        engine = _get_guest_engine(client)
        if engine is None:
            return None
        bot_db.init_ai_tables()
        _AI_EMPLOYEE_INSTANCE = AiEmployee(client, engine)
        _AI_EMPLOYEE_INSTANCE.start_background_scheduler()
        logger.info("[ai_employee] singleton created with background scheduler")
    return _AI_EMPLOYEE_INSTANCE


async def _handle_ai_dm(client: TelegramBotClient, chat_id: int, text: str, update: dict[str, Any]) -> None:
    """Handle a DM by routing through the AI engine for a natural response."""
    try:
        employee = _get_ai_employee(client)
        if employee:
            await employee.process_group_message(update)
            return
    except Exception:
        logger.exception("AI employee failed, sending fallback")

    await client.send_message(chat_id, "잠시 후 다시 시도해주세요. 😊")


async def _handle_menu_command(client: TelegramBotClient, chat_id: int) -> None:
    await client.send_message(
        chat_id,
        "📋 **명령어 목록**\n\n아래 버튼을 눌러 원하는 기능을 선택하세요.",
        reply_markup={"keyboard": _MENU_COMMANDS, "resize_keyboard": True, "one_time_keyboard": False},
    )


async def _handle_menu_button(client: TelegramBotClient, chat_id: int, button_text: str) -> None:
    cmd = _COMMAND_MAP.get(button_text)
    if cmd and cmd in _MENU_RESPONSES:
        await client.send_message(chat_id, _MENU_RESPONSES[cmd])
    else:
        await client.send_message(chat_id, "죄송합니다. 해당 명령어를 이해할 수 없습니다. 😅\n다시 `/menu`를 입력해보세요.")


async def _remove_menu_keyboard(client: TelegramBotClient, chat_id: int) -> None:
    await client.send_message(chat_id, "자유롭게 질문해주세요 ✨", reply_markup={"remove_keyboard": True})


# ── Stars Payment Helpers ────────────────────────────────────────────

_STAR_PRODUCTS: dict[str, dict[str, Any]] = {
    "pro_monthly": {"title": "Pro 월간 구독", "star_amount": 1500, "plan": "pro", "period_days": 30, "label": "Pro", "ai_calls": None},
    "pro_yearly": {"title": "Pro 연간 구독 (20% 할인)", "star_amount": 12000, "plan": "pro", "period_days": 365, "label": "Pro 연간", "ai_calls": None},
    "team_monthly": {"title": "Team 월간 구독", "star_amount": 4500, "plan": "team", "period_days": 30, "label": "Team", "ai_calls": None},
    "ai_boost_1000": {"title": "AI Boost — 1,000회", "star_amount": 300, "plan": None, "period_days": None, "label": "AI Boost", "ai_calls": 1000},
    "ai_boost_5000": {"title": "AI Boost — 5,000회 (20% 할인)", "star_amount": 1200, "plan": None, "period_days": None, "label": "AI Boost+", "ai_calls": 5000},
}


async def _handle_pre_checkout_query(client: TelegramBotClient, query: dict[str, Any]) -> None:
    query_id = query.get("id")
    payload_str = query.get("invoice_payload", "{}")
    try:
        payload = json.loads(payload_str)
        product_id = payload.get("pid")
        if product_id not in _STAR_PRODUCTS:
            logger.warning("[stars] invalid product in payload: %s", product_id)
            await client.answer_pre_checkout_query(query_id, ok=False, error_message="Invalid product.")
            return
        await client.answer_pre_checkout_query(query_id, ok=True)
        logger.info("[stars] pre_checkout_query approved: %s", product_id)
    except Exception as e:
        logger.error("[stars] pre_checkout_query error: %s", e)
        try:
            await client.answer_pre_checkout_query(query_id, ok=False, error_message="Processing error.")
        except Exception:
            pass


async def _handle_successful_payment(client: TelegramBotClient, message: dict[str, Any]) -> None:
    payment = message.get("successful_payment", {})
    payload_str = payment.get("invoice_payload", "{}")
    telegram_charge_id = payment.get("telegram_payment_charge_id", "")
    stars_amount = payment.get("total_amount", 0)

    try:
        payload = json.loads(payload_str)
        product_id = payload.get("pid")
        user_id = payload.get("uid")
        if not product_id or not user_id:
            logger.warning("[stars] incomplete payment payload: %s", payload_str)
            return

        product = _STAR_PRODUCTS.get(product_id)
        if not product:
            logger.warning("[stars] unknown product: %s", product_id)
            return

        admin = AdminPlatform.get_instance()

        if product.get("plan") and product.get("period_days"):
            admin.change_plan(user_id, product["plan"])
            admin.create_subscription(user_id=user_id, plan=product["plan"])
            admin._audit(user_id, "stars_payment", AuditAction.PAYMENT_SUCCEEDED, "subscription", user_id, {"product": product_id, "plan": product["plan"], "stars": stars_amount, "charge_id": telegram_charge_id})
            logger.info("[stars] payment: user=%s → %s (%d Stars)", user_id, product["plan"], stars_amount)
        elif product.get("ai_calls"):
            admin.record_usage(user_id=user_id, api_calls=0)
            admin._audit(user_id, "stars_payment", AuditAction.PAYMENT_SUCCEEDED, "ai_boost", user_id, {"product": product_id, "ai_calls": product["ai_calls"], "stars": stars_amount})

        admin.create_invoice(user_id=user_id, amount_cents=stars_amount * 100, stripe_invoice_id=telegram_charge_id)

        if product.get("plan") and product.get("period_days"):
            async with async_session_maker() as db:
                from sqlalchemy import select
                phone = f"tg_{user_id}"
                result = await db.execute(select(Tenant).where(Tenant.phone == phone))
                tenant = result.scalar_one_or_none()
                if tenant is not None and tenant.referred_by and not tenant.referral_rewarded:
                    referrer = await db.get(Tenant, tenant.referred_by)
                    if referrer is not None:
                        commission_cents = int(stars_amount * 0.10)
                        db.add(ReferralCommission(referrer_id=referrer.id, referred_id=tenant.id, amount_cents=commission_cents, rate=10, status="pending"))
                        referrer.referral_earnings = (referrer.referral_earnings or 0) + commission_cents
                        tenant.referral_rewarded = True
                        await db.commit()
                        logger.info("stars_referral_commission_credited", referrer_tenant_id=referrer.id, referred_tenant_id=tenant.id, commission_cents=commission_cents)
    except Exception as e:
        logger.error("[stars] successful_payment error: %s", e)


async def _remove_menu_keyboard_deferred(client: TelegramBotClient, chat_id: int) -> None:
    try:
        await client.send_message(chat_id, "자유롭게 질문해주세요 ✨", reply_markup={"remove_keyboard": True})
    except Exception:
        pass


# ── Main Update Handler ──────────────────────────────────────────────


async def handle_update(update: dict[str, Any]) -> None:
    client = _client()
    if not client:
        logger.warning("Telegram bot update received but TELEGRAM_BOT_TOKEN is not configured")
        return

    try:
        # Guest Message (Bot API 10.0+)
        if "guest_message" in update:
            engine = _get_guest_engine(client)
            if engine:
                await engine.handle_guest_message(update)
            return

        # Regular Message
        if "message" in update:
            message = update["message"]
            chat_id = message.get("chat", {}).get("id")
            text = message.get("text", "")
            chat_type = message.get("chat", {}).get("type", "")

            if "successful_payment" in message:
                await _handle_successful_payment(client, message)
                return

            if chat_id is None:
                return

            # /start → 인사 + AI chat (또는 verify token)
            if text.startswith("/start"):
                parts = text.split(maxsplit=1)
                has_token = len(parts) > 1 and parts[1].strip()
                if has_token:
                    await _handle_verify_start(client, chat_id, message)
                else:
                    await client.send_message(chat_id, _GREETING)
                    await _remove_menu_keyboard(client, chat_id)
                return

            # /menu → 키보드 메뉴 표시
            if text.strip().lower() in ("/menu", "메뉴", "menu"):
                await _handle_menu_command(client, chat_id)
                return

            # 메뉴 버튼 클릭 처리
            if text in _COMMAND_MAP:
                await _handle_menu_button(client, chat_id, text)
                return

            # 그룹 @멘션 → AiEmployee
            if chat_type in ("group", "supergroup"):
                if AiEmployee._is_bot_mentioned(text):
                    employee = _get_ai_employee(client)
                    if employee:
                        await employee.process_group_message(update)
                return

            # 1:1 채팅 → AI 자연어 응답
            await _handle_ai_dm(client, chat_id, text, update)
            return

        # Callback Query
        if "callback_query" in update:
            callback_query = update["callback_query"]
            if callback_query.get("data", "").startswith("verify:"):
                await _handle_verify_callback(client, callback_query)
            return

        # Pre-checkout Query (Stars Payment)
        if "pre_checkout_query" in update:
            await _handle_pre_checkout_query(client, update["pre_checkout_query"])
            return

    except Exception:
        logger.exception("Error while handling Telegram update")
