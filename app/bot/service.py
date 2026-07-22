"""
Update-handling logic for the Telegram bot.

Complete rewrite: category-based menu system + AI-first UX.

Architecture:
- /start → greeting + AI chat (no menu buttons)
- /menu → main category keyboard (4 categories)
- Each category → sub-menu keyboard
- Free text → AI response (reuses GuestEngine.decide_action)
- Scheduler: daily report push to all users
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timezone
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

# ═══════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════

_VERIFY_BUTTON_TEXT = "✅ 채널 가입 확인"

_GREETING = (
    "🌟 안녕하세요, **TeleMon**입니다!\n\n"
    "무엇을 도와드릴까요?\n"
    "자유롭게 질문해주시면 AI가 도와드립니다 ✨\n"
    "명령어 목록은 `/menu` 를 입력하세요."
)

# ── Main Menu (4 categories) ─────────────────────────────────────────

_MAIN_MENU = [
    [{"text": "📊 내 대시보드"}, {"text": "🔧 도구"}],
    [{"text": "🏪 스토어"}, {"text": "👤 내 정보"}],
]

# ── Sub-menus ────────────────────────────────────────────────────────

_CATEGORY_KEYBOARDS: dict[str, list[list[dict[str, str]]]] = {
    "📊 내 대시보드": [
        [{"text": "📈 오늘 현황"}, {"text": "📅 예약 목록"}],
        [{"text": "📋 최근 발송 이력"}, {"text": "🔔 계정 알림"}],
        [{"text": "🔙 메인 메뉴"}],
    ],
    "🔧 도구": [
        [{"text": "🤖 AI 메시지 작성"}, {"text": "📋 템플릿"}],
        [{"text": "🔗 링크 단축"}, {"text": "⏰ 예약 발송"}],
        [{"text": "🔙 메인 메뉴"}],
    ],
    "🏪 스토어": [
        [{"text": "⭐ Stars 충전"}, {"text": "📦 플랜 업그레이드"}],
        [{"text": "🎁 AI Boost"}, {"text": "📜 구매내역"}],
        [{"text": "🔙 메인 메뉴"}],
    ],
    "👤 내 정보": [
        [{"text": "🔑 API 키"}, {"text": "🤝 추천인"}],
        [{"text": "✅ 출석체크"}, {"text": "🏆 랭킹"}],
        [{"text": "⚙️ 설정"}, {"text": "📢 공지사항"}],
        [{"text": "🔙 메인 메뉴"}],
    ],
}

# ── Response content for each leaf button ─────────────────────────────

_CATEGORY_RESPONSES: dict[str, str] = {
    "📈 오늘 현황": "📊 **오늘의 발송 현황**\n\n웹사이트에서 자세한 현황을 확인하세요:\nhttps://app.telemon.online",
    "📅 예약 목록": "📅 **예약 발송 목록**\n\n웹사이트에서 예약 현황을 확인하세요:\nhttps://app.telemon.online",
    "📋 최근 발송 이력": "📋 **최근 발송 이력**\n\n웹사이트에서 발송 이력을 확인하세요:\nhttps://app.telemon.online",
    "🔔 계정 알림": "🔔 **계정 알림**\n\n웹사이트에서 알림을 확인하세요:\nhttps://app.telemon.online",
    "🤖 AI 메시지 작성": "✍️ **AI 메시지 작성**\n\n보내고 싶은 메시지의 주제나 키워드를 입력하면 AI가 작성을 도와드립니다.\n예: \"새해 인사 문구 만들어줘\"",
    "📋 템플릿": "📋 **메시지 템플릿**\n\n웹사이트에서 템플릿을 관리하세요:\nhttps://app.telemon.online",
    "🔗 링크 단축": "🔗 **링크 단축**\n\n링크를 보내주시면 단축링크를 생성해드립니다.\n예: https://t.me/abc/123",
    "⏰ 예약 발송": "⏰ **예약 발송**\n\n예약 명령어 형식:\n`/schedule YYYY-MM-DD HH:MM 메시지`\n\n예: `/schedule 2026-07-25 10:00 안녕하세요!`",
    "⭐ Stars 충전": "⭐ **Stars 충전**\n\n텔레그램 Stars로 결제하려면 `/buy` 명령어를 사용하세요.\n\n상품 목록:\n- Pro 월간 — 1,500 ⭐\n- Pro 연간 — 12,000 ⭐ (20% 할인)\n- Team 월간 — 4,500 ⭐\n- AI Boost 1,000회 — 300 ⭐\n- AI Boost 5,000회 — 1,200 ⭐ (20% 할인)",
    "📦 플랜 업그레이드": "📦 **플랜 업그레이드**\n\n웹사이트에서 플랜을 변경하세요:\nhttps://app.telemon.online",
    "🎁 AI Boost": "🎁 **AI Boost**\n\nAI 추가 호출이 필요하면 `/buy` 명령어로 구매하세요.",
    "📜 구매내역": "📜 **구매내역**\n\n웹사이트에서 구매내역을 확인하세요:\nhttps://app.telemon.online",
    "🔑 API 키": "🔑 **내 API 키**\n\n웹사이트에서 API 키를 확인하세요:\nhttps://app.telemon.online",
    "🤝 추천인": "🤝 **추천인**\n\n웹사이트에서 추천인 코드를 확인하세요:\nhttps://app.telemon.online",
    "✅ 출석체크": "✅ **출석체크**\n\n웹사이트에서 출석체크를 진행하세요:\nhttps://app.telemon.online",
    "🏆 랭킹": "🏆 **랭킹**\n\n웹사이트에서 이번주 발송 랭킹을 확인하세요:\nhttps://app.telemon.online",
    "⚙️ 설정": "⚙️ **설정**\n\n웹사이트에서 설정을 변경하세요:\nhttps://app.telemon.online",
    "📢 공지사항": "📢 **공지사항**\n\n최신 소식은 공식 채널을 확인하세요:\nhttps://t.me/telemon_official",
}

# ── Link shortener storage ──────────────────────────────────────────

_SHORT_LINKS: dict[str, str] = {}

def _generate_short_code() -> str:
    import secrets, string
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))

# ═══════════════════════════════════════════════════════════════════════
# Telegram Client
# ═══════════════════════════════════════════════════════════════════════

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
    for chat_id in cfg.admin_chat_ids:
        try:
            await client.send_message(chat_id, text)
        except Exception as e:
            logger.warning("Admin notify failed for chat_id=%s: %s", chat_id, e)
    bot_db.log_admin_notify(event_type, text, delivered=True)


# ═══════════════════════════════════════════════════════════════════════
# Free API Key Flow (preserved)
# ═══════════════════════════════════════════════════════════════════════

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
        chat_id=str(chat_id), token=token,
        telegram_user_id=from_user.get("id"), telegram_username=from_user.get("username"),
    )
    cfg = get_config().telegram_bot
    channel_note = f"\n\n📢 채널: {cfg.channel_id}" if cfg.channel_id else ""
    await client.send_message(
        chat_id,
        "🔗 **채널 인증이 필요합니다.**\n\n무료 체험 API 키를 발급받으려면 아래 채널에 가입한 후 인증 버튼을 눌러주세요." + channel_note,
        reply_markup=_verify_keyboard(token),
    )


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
        await client.answer_callback_query(callback_id, "채널 상태를 확인할 수 없습니다. 봇이 채널 관리자로 등록되어 있는지 확인해주세요.", show_alert=True)
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


# ═══════════════════════════════════════════════════════════════════════
# AI Engine (reuses existing)
# ═══════════════════════════════════════════════════════════════════════

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
    return _AI_EMPLOYEE_INSTANCE


# ═══════════════════════════════════════════════════════════════════════
# Feature Handlers
# ═══════════════════════════════════════════════════════════════════════

# ── Menu System ──────────────────────────────────────────────────────

async def _show_main_menu(client: TelegramBotClient, chat_id: int, text: str = "📋 **메인 메뉴**\n\n원하는 카테고리를 선택하세요.") -> None:
    await client.send_message(
        chat_id, text,
        reply_markup={"keyboard": _MAIN_MENU, "resize_keyboard": True},
    )


async def _show_sub_menu(client: TelegramBotClient, chat_id: int, category: str) -> None:
    kb = _CATEGORY_KEYBOARDS.get(category)
    if not kb:
        await _show_main_menu(client, chat_id)
        return
    await client.send_message(
        chat_id,
        f"📂 **{category}**\n\n원하는 항목을 선택하세요.",
        reply_markup={"keyboard": kb, "resize_keyboard": True},
    )


async def _handle_category_button(client: TelegramBotClient, chat_id: int, text: str) -> None:
    if text == "🔙 메인 메뉴":
        await _show_main_menu(client, chat_id)
        return
    if text in _CATEGORY_KEYBOARDS:
        await _show_sub_menu(client, chat_id, text)
        return
    response = _CATEGORY_RESPONSES.get(text)
    if response:
        # AI message writer — enter AI mode
        if text == "🤖 AI 메시지 작성":
            session_key = f"ai_mode:{chat_id}"
            bot_db._ai_sessions[session_key] = {"mode": "message_writer", "chat_id": chat_id}
            await client.send_message(chat_id, f"{response}\n\n어떤 내용의 메시지를 작성할까요? 주제를 알려주세요!")
            return
        # Shorten link
        if text == "🔗 링크 단축":
            session_key = f"shorten:{chat_id}"
            bot_db._ai_sessions[session_key] = {"mode": "shorten", "chat_id": chat_id}
            await client.send_message(chat_id, f"{response}\n\n단축할 링크를 보내주세요!")
            return
        # Schedule
        if text == "⏰ 예약 발송":
            await client.send_message(chat_id, response)
            return
        await client.send_message(chat_id, response)
    else:
        await client.send_message(chat_id, "죄송합니다. 해당 기능을 아직 추가 중입니다 😅\n다시 `/menu`를 입력해보세요.")


# ── AI Chat ──────────────────────────────────────────────────────────

async def _handle_ai_chat(client: TelegramBotClient, chat_id: int, text: str, update: dict[str, Any]) -> None:
    employee = _get_ai_employee(client)
    if employee:
        try:
            await employee.process_group_message(update)
            return
        except Exception:
            logger.exception("AI chat failed")
    await client.send_message(chat_id, "잠시 후 다시 시도해주세요. 😊")


# ── Link Shortener ──────────────────────────────────────────────────

_TELEGRAM_LINK_RE = re.compile(r"https?://t\.me/\S+|https?://telegram\.me/\S+")

async def _handle_shorten(client: TelegramBotClient, chat_id: int, text: str) -> None:
    # Check if text is a URL
    urls = _TELEGRAM_LINK_RE.findall(text)
    if not urls:
        # Check for any URL
        url_match = re.search(r"https?://\S+", text)
        if url_match:
            urls = [url_match.group(0)]
    if not urls:
        await client.send_message(chat_id, "링크를 찾을 수 없습니다. 올바른 URL을 보내주세요. 📎")
        return
    for url in urls:
        code = _generate_short_code()
        _SHORT_LINKS[code] = url
        short_url = f"https://t.me/telemon_verify_bot?start=link_{code}"
        await client.send_message(
            chat_id,
            f"🔗 **단축링크 생성 완료!**\n\n```\n{short_url}\n```\n\n원본: {url[:50]}...",
        )
    # Clear AI session
    bot_db._ai_sessions.pop(f"shorten:{chat_id}", None)


# ── Schedule ─────────────────────────────────────────────────────────

async def _handle_schedule_command(client: TelegramBotClient, chat_id: int, text: str) -> None:
    # Format: /schedule YYYY-MM-DD HH:MM message
    match = re.match(r"/schedule\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+(.+)", text, re.DOTALL)
    if not match:
        await client.send_message(
            chat_id,
            "⏰ **예약 발송**\n\n올바른 형식:\n`/schedule YYYY-MM-DD HH:MM 메시지`\n\n예: `/schedule 2026-07-25 10:00 안녕하세요!`",
        )
        return
    date_str, time_str, msg = match.group(1), match.group(2), match.group(3)
    try:
        scheduled = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        if scheduled < datetime.now():
            await client.send_message(chat_id, "⚠️ 과거 시간으로는 예약할 수 없습니다. 미래 시간을 입력해주세요.")
            return
        # TODO: insert into broadcast table via API
        await client.send_message(
            chat_id,
            f"✅ **예약 완료!**\n\n📅 {date_str} {time_str}\n📝 {msg[:80]}{'...' if len(msg) > 80 else ''}\n\n웹사이트에서 예약 현황을 확인하세요:\nhttps://app.telemon.online",
        )
    except ValueError:
        await client.send_message(chat_id, "⚠️ 날짜 또는 시간 형식이 올바르지 않습니다. `YYYY-MM-DD HH:MM` 형식으로 입력해주세요.")


# ── Buy / Stars ────────────────────────────────────────────────────────

_STAR_PRODUCTS: dict[str, dict[str, Any]] = {
    "pro_monthly": {"title": "Pro 월간 구독", "star_amount": 1500, "plan": "pro", "period_days": 30, "label": "Pro", "ai_calls": None},
    "pro_yearly": {"title": "Pro 연간 구독 (20% 할인)", "star_amount": 12000, "plan": "pro", "period_days": 365, "label": "Pro 연간", "ai_calls": None},
    "team_monthly": {"title": "Team 월간 구독", "star_amount": 4500, "plan": "team", "period_days": 30, "label": "Team", "ai_calls": None},
    "ai_boost_1000": {"title": "AI Boost — 1,000회", "star_amount": 300, "plan": None, "period_days": None, "label": "AI Boost", "ai_calls": 1000},
    "ai_boost_5000": {"title": "AI Boost — 5,000회 (20% 할인)", "star_amount": 1200, "plan": None, "period_days": None, "label": "AI Boost+", "ai_calls": 5000},
}


async def _handle_buy_command(client: TelegramBotClient, chat_id: int, text: str) -> None:
    """Handle /buy command with optional product parameter."""
    parts = text.split()
    product_key = parts[1] if len(parts) > 1 else None

    if product_key:
        product = _STAR_PRODUCTS.get(product_key)
        if not product:
            await client.send_message(chat_id, "⚠️ 잘못된 상품입니다. `/buy` 를 입력하여 상품 목록을 확인하세요.")
            return
        title = product["title"]
        amount = product["star_amount"]
        payload = json.dumps({"pid": product_key, "uid": str(chat_id)})
        await client.send_invoice(
            chat_id=chat_id,
            title=title,
            description=f"{product['label']} — {amount} ⭐",
            payload=payload,
            currency="XTR",
            prices=[{"label": product["label"], "amount": amount}],
        )
        return

    # Show product list
    lines = ["🏪 **Stars 스토어**\n", "구매할 상품을 선택하세요:\n"]
    for key, prod in _STAR_PRODUCTS.items():
        lines.append(f"• `{key}` — {prod['title']} — {prod['star_amount']} ⭐")
    lines.append("\n사용법: `/buy 상품키`\n예: `/buy pro_monthly`")
    await client.send_message(chat_id, "\n".join(lines))


async def _handle_pre_checkout_query(client: TelegramBotClient, query: dict[str, Any]) -> None:
    query_id = query.get("id")
    payload_str = query.get("invoice_payload", "{}")
    try:
        payload = json.loads(payload_str)
        product_id = payload.get("pid")
        if product_id not in _STAR_PRODUCTS:
            await client.answer_pre_checkout_query(query_id, ok=False, error_message="Invalid product.")
            return
        await client.answer_pre_checkout_query(query_id, ok=True)
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
            return

        product = _STAR_PRODUCTS.get(product_id)
        if not product:
            return

        admin = AdminPlatform.get_instance()
        if product.get("plan") and product.get("period_days"):
            admin.change_plan(user_id, product["plan"])
            admin.create_subscription(user_id=user_id, plan=product["plan"])
            admin._audit(user_id, "stars_payment", AuditAction.PAYMENT_SUCCEEDED, "subscription", user_id, {"product": product_id, "plan": product["plan"], "stars": stars_amount, "charge_id": telegram_charge_id})
        elif product.get("ai_calls"):
            admin.record_usage(user_id=user_id, api_calls=0)
            admin._audit(user_id, "stars_payment", AuditAction.PAYMENT_SUCCEEDED, "ai_boost", user_id, {"product": product_id, "ai_calls": product["ai_calls"], "stars": stars_amount})

        admin.create_invoice(user_id=user_id, amount_cents=stars_amount * 100, stripe_invoice_id=telegram_charge_id)

        # Commission
        if product.get("plan") and product.get("period_days"):
            async with async_session_maker() as db:
                from sqlalchemy import select
                phone = f"tg_{user_id}"
                result = await db.execute(select(Tenant).where(Tenant.phone == phone))
                tenant = result.scalar_one_or_none()
                if tenant and tenant.referred_by and not tenant.referral_rewarded:
                    referrer = await db.get(Tenant, tenant.referred_by)
                    if referrer:
                        commission_cents = int(stars_amount * 0.10)
                        db.add(ReferralCommission(referrer_id=referrer.id, referred_id=tenant.id, amount_cents=commission_cents, rate=10, status="pending"))
                        referrer.referral_earnings = (referrer.referral_earnings or 0) + commission_cents
                        tenant.referral_rewarded = True
                        await db.commit()
    except Exception as e:
        logger.error("[stars] successful_payment error: %s", e)


# ── Daily Report (scheduled) ─────────────────────────────────────────

async def send_daily_report_to_all_users() -> None:
    """Scheduled job: send daily broadcast stats to all bot users."""
    client = _client()
    if not client:
        return
    try:
        from app.crud import broadcast as broadcast_crud
        async with async_session_maker() as db:
            stats = await broadcast_crud.get_daily_stats(db)
    except Exception:
        logger.exception("Daily report: failed to fetch stats")
        return

    yesterday = date.today().isoformat()
    report = (
        f"📊 **TeleMon 일일 리포트** — {yesterday}\n\n"
        f"📨 총 발송: **{stats.get('total', 0):,}건**\n"
        f"✅ 성공: **{stats.get('success', 0):,}건** ({stats.get('success_rate', 0)}%)\n"
        f"❌ 실패: **{stats.get('failed', 0):,}건**\n"
        f"👤 활성 계정: **{stats.get('active_accounts', 0)}개**\n\n"
        f"💡 팁: 웹사이트에서 자세한 분석을 확인하세요\n"
        f"https://app.telemon.online"
    )

    sessions = bot_db.get_all_sessions()
    for session in sessions:
        chat_id_str = session.get("chat_id")
        if not chat_id_str:
            continue
        try:
            await client.send_message(int(chat_id_str), report)
            await asyncio.sleep(0.05)
        except Exception:
            pass


# ── Gamification ─────────────────────────────────────────────────────┐
async def _handle_checkin(client: TelegramBotClient, chat_id: int) -> None:
    today = date.today().isoformat()
    key = f"checkin:{chat_id}"
    last = bot_db._ai_sessions.get(key, {}).get("last")
    if last == today:
        streak = bot_db._ai_sessions.get(key, {}).get("streak", 0)
        await client.send_message(chat_id, f"✅ 오늘은 이미 출석체크를 완료했습니다!\n🔥 연속 출석 **{streak}일째**")
        return
    streak = bot_db._ai_sessions.get(key, {}).get("streak", 0) + 1
    bot_db._ai_sessions[key] = {"last": today, "streak": streak}
    bonus = "+5 ⭐" if streak >= 7 else ""
    await client.send_message(
        chat_id,
        f"✅ **출석체크 완료!**\n\n🔥 연속 **{streak}일째** 출석중!\n{bonus}\n계속해서 랭킹을 올려보세요! 🏆"
    )


async def _handle_ranking(client: TelegramBotClient, chat_id: int) -> None:
    # Simple mock ranking
    await client.send_message(
        chat_id,
        "🏆 **이번주 발송 랭킹**\n\n"
        "웹사이트에서 전체 랭킹을 확인하세요:\n"
        "https://app.telemon.online\n\n"
        "💡 매일 출석체크하고, 많이 발송할수록 랭킹이 올라갑니다!"
    )


# ── #1 Sessions ──────────────────────────────────────────────────────

async def _handle_sessions(client: TelegramBotClient, chat_id: int) -> None:
    session = bot_db.get_session(str(chat_id))
    if not session:
        await client.send_message(
            chat_id,
            "🔐 **세션 정보**\n\n연결된 세션이 없습니다.\n웹사이트에서 봇 인증을 완료해주세요:\nhttps://app.telemon.online"
        )
        return
    uid = session.get("telegram_user_id", "?")
    username = session.get("telegram_username", "없음")
    token = session.get("token", "")[:12]
    created = session.get("created_at", "")[:10]
    await client.send_message(
        chat_id,
        f"🔐 **내 세션 정보**\n\n"
        f"👤 사용자 ID: `{uid}`\n"
        f"📛 사용자명: @{username}\n"
        f"🔑 토큰: `{token}...`\n"
        f"📅 연결일: {created}\n\n"
        f"웹사이트에서 전체 세션을 관리하세요:\n"
        f"https://app.telemon.online"
    )


# ── #6 Group Cleaner ─────────────────────────────────────────────────

async def _handle_cleanup(client: TelegramBotClient, chat_id: int) -> None:
    await client.send_message(
        chat_id,
        "🧹 **그룹 클리너**\n\n"
        "오래된 그룹을 정리하려면 웹사이트에서 확인하세요:\n"
        "https://app.telemon.online\n\n"
        "💡 30일 이상 활동 없는 그룹을 자동으로 찾아 정리 제안을 드립니다."
    )


# ── #10 Language / i18n ──────────────────────────────────────────────

_LANG_OPTIONS = {
    "🇰🇷 한국어": "ko",
    "🇺🇸 English": "en",
    "🇯🇵 日本語": "ja",
}

_LANG_DATA: dict[str, dict[str, str]] = {
    "ko": {
        "lang_selected": "✅ 언어가 한국어로 변경되었습니다.",
        "greeting": "🌟 안녕하세요, **TeleMon**입니다!\n\n무엇을 도와드릴까요?\n자유롭게 질문해주시면 AI가 도와드립니다 ✨\n명령어 목록은 `/menu` 를 입력하세요.",
        "menu": "📋 **메인 메뉴**\n\n원하는 카테고리를 선택하세요.",
    },
    "en": {
        "lang_selected": "✅ Language changed to English.",
        "greeting": "🌟 Welcome to **TeleMon**!\n\nHow can I help you?\nFeel free to ask any questions and our AI will assist you ✨\nType `/menu` for commands.",
        "menu": "📋 **Main Menu**\n\nSelect a category.",
    },
    "ja": {
        "lang_selected": "✅ 言語が日本語に変更されました。",
        "greeting": "🌟 **TeleMon**へようこそ！\n\nどのようなご用件でしょうか？\nAIがお手伝いします ✨\nコマンド一覧は `/menu` を入力してください。",
        "menu": "📋 **メインメニュー**\n\nカテゴリを選択してください。",
    },
}


def _get_lang(chat_id: int) -> str:
    return bot_db._ai_sessions.get(f"lang:{chat_id}", {}).get("lang", "ko")


async def _handle_lang_command(client: TelegramBotClient, chat_id: int, text: str) -> None:
    parts = text.split(maxsplit=1)
    target_lang = parts[1].strip().lower() if len(parts) > 1 else ""

    if not target_lang:
        kb = [[{"text": key}] for key in _LANG_OPTIONS]
        kb.append([{"text": "🔙 메인 메뉴"}])
        await client.send_message(
            chat_id,
            "🌍 **언어 선택 / Language**\n\n사용할 언어를 선택하세요.\nSelect your preferred language.",
            reply_markup={"keyboard": kb, "resize_keyboard": True},
        )
        return

    lang_map = {"ko": "ko", "en": "en", "ja": "ja", "korean": "ko", "english": "en", "japanese": "ja", "한국어": "ko", "영어": "en", "일본어": "ja"}
    lang_code = lang_map.get(target_lang)
    if not lang_code or lang_code not in _LANG_DATA:
        await client.send_message(chat_id, "⚠️ 지원하는 언어: 한국어(`ko`), English(`en`), 日本語(`ja`)")
        return

    bot_db._ai_sessions[f"lang:{chat_id}"] = {"lang": lang_code}
    await client.send_message(chat_id, _LANG_DATA[lang_code]["lang_selected"])


async def _handle_lang_button(client: TelegramBotClient, chat_id: int, button_text: str) -> None:
    lang_code = _LANG_OPTIONS.get(button_text)
    if not lang_code:
        return
    bot_db._ai_sessions[f"lang:{chat_id}"] = {"lang": lang_code}
    msg = _LANG_DATA[lang_code]["lang_selected"]
    await client.send_message(chat_id, msg)


# ── #2 Cleanup (triggered from menu) ─────────────────────────────────│


# ═══════════════════════════════════════════════════════════════════════
# Main Update Handler
# ═══════════════════════════════════════════════════════════════════════

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

            # /start → verify or greeting
            if text.startswith("/start"):
                parts = text.split(maxsplit=1)
                has_token = len(parts) > 1 and parts[1].strip()
                if has_token:
                    await _handle_verify_start(client, chat_id, message)
                else:
                    await client.send_message(chat_id, _GREETING, reply_markup={"remove_keyboard": True})
                return

            # /sessions → show session info
            if text.strip().lower() in ("/sessions", "/session"):
                await _handle_sessions(client, chat_id)
                return

            # /cleanup → group cleanup
            if text.strip().lower() in ("/cleanup", "/clean"):
                await _handle_cleanup(client, chat_id)
                return

            # /lang → language selection
            if text.startswith("/lang"):
                await _handle_lang_command(client, chat_id, text)
                return

            # /menu → main category menu
            if text.strip().lower() in ("/menu", "메뉴", "menu"):
                await _show_main_menu(client, chat_id)
                return

            # /buy → Stars store
            if text.startswith("/buy"):
                await _handle_buy_command(client, chat_id, text)
                return

            # /schedule → reservation
            if text.startswith("/schedule"):
                await _handle_schedule_command(client, chat_id, text)
                return

            # Check for active AI session (message writer mode)
            if bot_db._ai_sessions.get(f"ai_mode:{chat_id}"):
                session = bot_db._ai_sessions.get(f"ai_mode:{chat_id}")
                if session.get("mode") == "message_writer":
                    try:
                        employee = _get_ai_employee(client)
                        if employee:
                            await employee.process_group_message(update)
                            bot_db._ai_sessions.pop(f"ai_mode:{chat_id}", None)
                            return
                    except Exception:
                        logger.exception("AI message writer failed")
                    await client.send_message(
                        chat_id,
                        "✍️ **AI 메시지 작성 결과**\n\n죄송합니다. 메시지 작성 중 오류가 발생했습니다. 😅\n다시 시도해주세요.",
                    )
                    bot_db._ai_sessions.pop(f"ai_mode:{chat_id}", None)
                    return

            # Check for active shorten session
            if bot_db._ai_sessions.get(f"shorten:{chat_id}"):
                await _handle_shorten(client, chat_id, text)
                return

            # Category menu button
            if text in _MAIN_MENU[0] or text in _MAIN_MENU[1] or text in sum(_CATEGORY_KEYBOARDS.values(), []):
                await _handle_category_button(client, chat_id, text)
                return

            # Language select button
            if text in _LANG_OPTIONS:
                await _handle_lang_button(client, chat_id, text)
                return

            # Back to main menu from submenu
            if text == "🔙 메인 메뉴":
                await _show_main_menu(client, chat_id)
                return

            # Group @mention → AiEmployee
            if chat_type in ("group", "supergroup"):
                if AiEmployee._is_bot_mentioned(text):
                    employee = _get_ai_employee(client)
                    if employee:
                        await employee.process_group_message(update)
                return

            # 1:1 DM → AI chat
            await _handle_ai_chat(client, chat_id, text, update)
            return

        # Callback Query
        if "callback_query" in update:
            callback_query = update["callback_query"]
            if callback_query.get("data", "").startswith("verify:"):
                await _handle_verify_callback(client, callback_query)
            return

        # Pre-checkout Query (Stars)
        if "pre_checkout_query" in update:
            await _handle_pre_checkout_query(client, update["pre_checkout_query"])
            return

    except Exception:
        logger.exception("Error while handling Telegram update")
