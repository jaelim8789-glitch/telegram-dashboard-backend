from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from app.config import settings
from app.core.logging import get_logger
from app.core.plans import PLAN_CATALOG
from app.crud import account as account_crud
from app.crud import telegram_verification as verification_crud
from app.database import async_session_maker
from app.models.tenant import PaymentRecord
from app.services import ai_chat_service, billing, bot_account_service, purchase_service
from app.services.auto_reply_service import AccountNotAuthenticatedError, disable_auto_reply, enable_auto_reply
from app.services.bot_account_service import AccountSnapshot, BotPurchaseResult, ClaimResult
from app.services.bot_api_key_service import handle_self_service_api_key

# Telegram user ids currently inside the "AI Chat" free-text conversation flow.
# Process-local, like bot_api_key_service._in_flight — the bot is a single
# polling instance, so a plain set is sufficient to gate the free-text handler.
_active_ai_chat_users: set[int] = set()

logger = get_logger(__name__)

_application: Application | None = None

_SUBSCRIPTION_STATUS_LABELS = {
    "active": "활성",
    "pending": "결제 대기중",
    "inactive": "비활성",
    "expired": "만료됨",
    "canceled": "취소됨",
}

_PAYMENT_STATUS_LABELS = {
    "completed": "완료 ✅",
    "pending": "대기중",
    "unmatched": "미확인",
    "failed": "실패",
}


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    """Top-level bot menu — the full self-service ops menu."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔑 내 API 키", callback_data="apikey:get")],
            [
                InlineKeyboardButton("💳 내 플랜/만료일", callback_data="plan:info"),
                InlineKeyboardButton("👤 계정 상태", callback_data="account:status"),
            ],
            [
                InlineKeyboardButton("💰 결제(업그레이드)", callback_data="pay:menu"),
                InlineKeyboardButton("🔄 갱신", callback_data="renew:start"),
            ],
            [InlineKeyboardButton("📜 구매내역", callback_data="purchase:history")],
            [InlineKeyboardButton("✅ 출석체크", callback_data="checkin:do")],
            [InlineKeyboardButton("🎁 추천인 프로그램", callback_data="referral:info")],
            [InlineKeyboardButton("⭐ Stars 충전", callback_data="starstopup:menu")],
            [InlineKeyboardButton("🤖 자동 응답 관리", callback_data="autoreply_menu")],
            [
                InlineKeyboardButton("🆘 고객센터", callback_data="support:info"),
                InlineKeyboardButton("📢 공지", callback_data="notice:info"),
            ],
            [InlineKeyboardButton("🤖 AI Chat", callback_data="aichat:start")],
        ]
    )


def _back_to_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀ 메인 메뉴", callback_data="menu:main")]])


def _pay_menu_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for plan_id, plan_def in PLAN_CATALOG.items():
        if plan_id == "free":
            continue
        billing = "monthly" if "monthly" in plan_def["prices_usdt"] else "quarterly"
        price = plan_def["prices_usdt"][billing]
        label = f"{plan_def['name']} — ${price} ({'월' if billing == 'monthly' else '분기'})"
        rows.append([InlineKeyboardButton(label, callback_data=f"pay:select:{plan_id}")])
    rows.append([InlineKeyboardButton("◀ 메인 메뉴", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def _pending_check_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 다시 확인", callback_data="pay:check")],
            [InlineKeyboardButton("◀ 메인 메뉴", callback_data="menu:main")],
        ]
    )


def _plan_info_text(snapshot: AccountSnapshot) -> str:
    if not snapshot.linked:
        return (
            "💳 아직 요금제 정보가 없습니다.\n"
            "'💰 결제(업그레이드)' 메뉴에서 요금제를 구매하거나, 공식 채널 가입 후 무료체험을 시작해주세요."
        )
    lines = [
        f"💳 현재 요금제: {snapshot.plan_name}",
        f"상태: {_SUBSCRIPTION_STATUS_LABELS.get(snapshot.subscription_status, snapshot.subscription_status)}",
    ]
    if snapshot.trial_expires_at:
        lines.append(f"무료체험 만료일: {snapshot.trial_expires_at.strftime('%Y-%m-%d %H:%M')}")
    if snapshot.billing_period_end:
        lines.append(f"결제 만료일: {snapshot.billing_period_end.strftime('%Y-%m-%d')}")
    return "\n".join(lines)


def _account_status_text(snapshot: AccountSnapshot) -> str:
    if not snapshot.linked:
        return "👤 연동된 TeleMon 계정이 없습니다.\n결제 또는 무료체험을 시작하면 이 텔레그램 계정으로 연동됩니다."
    lines = [
        "👤 계정 상태",
        f"요금제: {snapshot.plan_name} ({_SUBSCRIPTION_STATUS_LABELS.get(snapshot.subscription_status, snapshot.subscription_status)})",
        f"API 키 발급: {'✅ 발급됨' if snapshot.has_api_key else '❌ 미발급'}",
    ]
    if snapshot.max_accounts is not None:
        lines.append(f"최대 계정 수: {snapshot.max_accounts}개")
    if snapshot.monthly_message_limit is not None:
        lines.append(f"월 메시지 한도: {snapshot.monthly_message_limit:,}건")
    return "\n".join(lines)


def _invoice_text(result: BotPurchaseResult) -> str:
    billing_label = "월간" if result.billing == "monthly" else "분기"
    return (
        f"💰 {result.plan_name} 요금제 ({billing_label}) 결제 안내\n\n"
        f"1. 아래 주소로 **{result.amount_usdt} USDT(TRC20)**를 보내주세요.\n"
        f"`{result.wallet_address}`\n\n"
        f"2. 송금 메모(memo)에 반드시 아래 코드를 입력하세요.\n"
        f"`{result.payment_ref}`\n\n"
        f"3. 입금이 확인되면 자동으로 요금제가 활성화되고, 이 채팅으로 API 키가 전송됩니다.\n"
        f"⏳ 평균 처리 시간: 5~10분"
    )


def _claim_text(result: ClaimResult) -> str:
    if result.status == "claimed":
        return (
            "✅ API 키가 발급되었습니다! 🎉\n\n"
            f"```\n{result.api_key}\n```\n\n"
            "⚠️ 이 키는 다시 표시되지 않습니다. 지금 안전한 곳에 저장해주세요."
        )
    return result.detail


def _history_text(records: list[PaymentRecord]) -> str:
    if not records:
        return "📜 구매 내역이 없습니다."
    lines = ["📜 구매 내역"]
    for record in records:
        date = record.created_at.strftime("%Y-%m-%d") if record.created_at else "-"
        amount = (record.amount_usdt or 0) / 100
        status_label = _PAYMENT_STATUS_LABELS.get(record.status, record.status)
        lines.append(f"• {date} | {record.plan or '-'} | ${amount:.2f} | {status_label}")
    return "\n".join(lines)


def _keyboard(accounts) -> InlineKeyboardMarkup:
    # One row per account rather than the single generic on/off pair from the original
    # spec — this dashboard manages up to a handful of accounts, and a bare "켜기/끄기"
    # pair gives no way to say *which* account, so each row picks a specific one.
    rows = []
    for account in accounts:
        label = account.name or account.phone
        rows.append(
            [
                InlineKeyboardButton(f"🔴 {label} 켜기", callback_data=f"autoreply:{account.id}:on"),
                InlineKeyboardButton(f"⚫ {label} 끄기", callback_data=f"autoreply:{account.id}:off"),
            ]
        )
    return InlineKeyboardMarkup(rows)


async def _status_message() -> tuple[str, InlineKeyboardMarkup]:
    async with async_session_maker() as db:
        accounts = await account_crud.list_accounts(db)
    if not accounts:
        return "등록된 계정이 없습니다. 먼저 대시보드에서 계정을 등록해주세요.", InlineKeyboardMarkup([])
    lines = ["📌 자동 응답 상태"] + [
        f"{a.name or a.phone}: {'켜짐' if a.auto_reply_enabled else '꺼짐'}" for a in accounts
    ]
    return "\n".join(lines), _keyboard(accounts)


async def autoreply_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = await _status_message()
    await update.message.reply_text(text, reply_markup=markup)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    # Route to the autoreply submenu
    if query.data == "autoreply_menu":
        await query.answer()
        text, markup = await _status_message()
        await query.edit_message_text(text, reply_markup=markup)
        return

    _, account_id, action = query.data.split(":", 2)

    try:
        if action == "on":
            await enable_auto_reply(account_id)
        else:
            await disable_auto_reply(account_id)
    except AccountNotAuthenticatedError as exc:
        await query.answer(text=str(exc), show_alert=True)
        return

    await query.answer()
    text, markup = await _status_message()
    await query.edit_message_text(text, reply_markup=markup)


async def apikey_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the "🔑 API 키 받기" button — self-service issuance / retrieval.

    The telegram_user_id comes from the Telegram Update (trusted), not from any
    HTTP request.  All eligibility, duplicate-prevention, and key-generation
    logic lives in app.services.bot_api_key_service.
    """
    query = update.callback_query
    await query.answer()

    telegram_user_id = update.effective_user.id if update.effective_user else None
    if telegram_user_id is None:
        await query.edit_message_text("⚠️ 사용자 정보를 확인할 수 없습니다.")
        return

    try:
        async with async_session_maker() as db:
            result = await handle_self_service_api_key(db, telegram_user_id)
    except Exception as exc:
        logger.error("bot_api_key_callback_failed", error=str(exc), telegram_user_id=telegram_user_id)
        await query.edit_message_text(
            "⚠️ 일시적인 서버 오류입니다. 잠시 후 다시 시도해주세요."
        )
        return

    # Build the reply based on the result status
    if result.status == "issued" and result.api_key:
        # Show the raw key once — this is the only time it will ever be visible.
        # Use a monospace block and a warning to save it.
        message = (
            f"✅ {result.detail}\n\n"
            f"```\n{result.api_key}\n```\n\n"
            f"⚠️ 이 키는 다시 표시되지 않습니다. 지금 안전한 곳에 저장해주세요."
        )
        await query.edit_message_text(message, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        # All non-issued outcomes: already_issued, not_linked, not_eligible,
        # payment_pending, server_error — just show the detail text.
        prefix = {
            "already_issued": "ℹ️",
            "not_linked": "🔗",
            "not_eligible": "🚫",
            "payment_pending": "⏳",
            "server_error": "⚠️",
        }.get(result.status, "⚠️")
        await query.edit_message_text(
            f"{prefix} {result.detail}",
            reply_markup=_main_menu_keyboard(),
        )


def _effective_telegram_user_id(update: Update) -> int | None:
    return update.effective_user.id if update.effective_user else None


async def plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle "💳 내 플랜/만료일" — read-only plan/expiry snapshot."""
    query = update.callback_query
    await query.answer()

    telegram_user_id = _effective_telegram_user_id(update)
    if telegram_user_id is None:
        await query.edit_message_text("⚠️ 사용자 정보를 확인할 수 없습니다.")
        return

    async with async_session_maker() as db:
        snapshot = await bot_account_service.get_account_snapshot(db, telegram_user_id)
    await query.edit_message_text(_plan_info_text(snapshot), reply_markup=_back_to_main_keyboard())


async def account_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle "👤 계정 상태" — read-only account/key-issuance snapshot."""
    query = update.callback_query
    await query.answer()

    telegram_user_id = _effective_telegram_user_id(update)
    if telegram_user_id is None:
        await query.edit_message_text("⚠️ 사용자 정보를 확인할 수 없습니다.")
        return

    async with async_session_maker() as db:
        snapshot = await bot_account_service.get_account_snapshot(db, telegram_user_id)
    await query.edit_message_text(_account_status_text(snapshot), reply_markup=_back_to_main_keyboard())


async def _reply_purchase_result(query, result: BotPurchaseResult) -> None:
    if result.status == "ok":
        markup = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ 입금 확인하기", callback_data="pay:check")],
                [InlineKeyboardButton("◀ 메인 메뉴", callback_data="menu:main")],
            ]
        )
        await query.edit_message_text(_invoice_text(result), parse_mode="Markdown", reply_markup=markup)
        return
    await query.edit_message_text(f"⚠️ {result.detail}", reply_markup=_back_to_main_keyboard())


async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle every "pay:*" button — plan selection menu, a chosen plan (invoice),
    and the "입금 확인하기" manual check/claim. All eligibility, plan validation,
    rate-limiting, and DB mutation lives in app.services.bot_account_service.
    """
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "menu":
        await query.answer()
        await query.edit_message_text("💰 결제할 요금제를 선택해주세요.", reply_markup=_pay_menu_keyboard())
        return

    telegram_user_id = _effective_telegram_user_id(update)
    if telegram_user_id is None:
        await query.answer()
        await query.edit_message_text("⚠️ 사용자 정보를 확인할 수 없습니다.")
        return

    if action == "select":
        plan = parts[2] if len(parts) > 2 else ""
        await query.answer()
        async with async_session_maker() as db:
            result = await bot_account_service.start_purchase(db, telegram_user_id, plan)
        await _reply_purchase_result(query, result)
        return

    if action == "check":
        await query.answer()
        async with async_session_maker() as db:
            claim = await bot_account_service.check_and_claim(db, telegram_user_id)
        markup = _pending_check_keyboard() if claim.status == "pending" else _back_to_main_keyboard()
        parse_mode = "Markdown" if claim.status == "claimed" else None
        await query.edit_message_text(_claim_text(claim), parse_mode=parse_mode, reply_markup=markup)
        return

    await query.answer()
    await query.edit_message_text("⚠️ 알 수 없는 요청입니다.", reply_markup=_main_menu_keyboard())


async def renew_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle "🔄 갱신" — repurchase the tenant's current plan, skipping selection."""
    query = update.callback_query
    await query.answer()

    telegram_user_id = _effective_telegram_user_id(update)
    if telegram_user_id is None:
        await query.edit_message_text("⚠️ 사용자 정보를 확인할 수 없습니다.")
        return

    async with async_session_maker() as db:
        result = await bot_account_service.start_renew(db, telegram_user_id)

    if result.status == "no_prior_plan":
        await query.edit_message_text(f"ℹ️ {result.detail}", reply_markup=_pay_menu_keyboard())
        return
    await _reply_purchase_result(query, result)


async def purchase_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle "📜 구매내역"."""
    query = update.callback_query
    await query.answer()

    telegram_user_id = _effective_telegram_user_id(update)
    if telegram_user_id is None:
        await query.edit_message_text("⚠️ 사용자 정보를 확인할 수 없습니다.")
        return

    async with async_session_maker() as db:
        records = await bot_account_service.list_purchase_history(db, telegram_user_id)
    await query.edit_message_text(_history_text(records), reply_markup=_back_to_main_keyboard())


async def checkin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle "✅ 출석체크" — once-per-day check-in, builds a streak, awards Stars."""
    query = update.callback_query
    await query.answer()

    telegram_user_id = _effective_telegram_user_id(update)
    if telegram_user_id is None:
        await query.edit_message_text("⚠️ 사용자 정보를 확인할 수 없습니다.")
        return

    async with async_session_maker() as db:
        result = await bot_account_service.do_checkin(db, telegram_user_id)
        leaderboard = await bot_account_service.get_checkin_leaderboard(db)

    lines = []
    if result.status == "no_tenant":
        lines.append(f"⚠️ {result.detail}")
    elif result.status == "already_checked_in":
        lines.append(f"✅ {result.detail}")
        lines.append(f"🔥 연속 출석: {result.streak}일")
    else:
        lines.append(f"🎉 출석 완료! +{result.stars_earned}⭐ 획득")
        lines.append(f"🔥 연속 출석: {result.streak}일 (보유 {result.stars_balance}⭐)")
        if result.streak % bot_account_service.CHECKIN_STREAK_MILESTONE_DAYS == 0:
            lines.append(f"🎁 {bot_account_service.CHECKIN_STREAK_MILESTONE_DAYS}일 연속 보너스 포함!")

    if leaderboard:
        lines.append("\n🏆 연속 출석 순위")
        lines.extend(f"{rank}위 · {streak}일" for rank, streak in leaderboard)

    await query.edit_message_text("\n".join(lines), reply_markup=_back_to_main_keyboard())


async def referral_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle "🎁 추천인 프로그램" — show the user's code, share link, and earnings."""
    query = update.callback_query
    await query.answer()

    telegram_user_id = _effective_telegram_user_id(update)
    if telegram_user_id is None:
        await query.edit_message_text("⚠️ 사용자 정보를 확인할 수 없습니다.")
        return

    async with async_session_maker() as db:
        tenant = await bot_account_service.get_referral_info(db, telegram_user_id)

    if tenant is None:
        await query.edit_message_text(
            "🎁 추천인 프로그램은 요금제를 시작한 후 이용할 수 있습니다.\n"
            "먼저 무료체험 또는 요금제를 시작해주세요.",
            reply_markup=_back_to_main_keyboard(),
        )
        return

    bot_username = settings.telegram_bot_username
    link = f"https://t.me/{bot_username}?start=ref_{tenant.referral_code}" if bot_username else None
    lines = [
        "🎁 추천인 프로그램",
        f"내 추천코드: `{tenant.referral_code}`",
    ]
    if link:
        lines.append(f"공유 링크: {link}")
    lines.append(f"\n누적 보상: ${tenant.referral_earnings / 100:.2f}")
    lines.append("친구가 이 링크로 가입 후 첫 결제를 완료하면 보상이 지급됩니다.")

    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=_back_to_main_keyboard()
    )


_STARS_TOPUP_PAYLOAD_PREFIX = "stars_topup:"


def _stars_topup_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"⭐ {amount:,}", callback_data=f"starstopup:buy:{amount}")]
        for amount in billing.STARS_TOPUP_PACKAGES
    ]
    rows.append([InlineKeyboardButton("◀ 메인 메뉴", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


async def starstopup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle "⭐ Stars 충전" — real Telegram Stars (XTR) purchase via the
    native Telegram payment sheet, credited 1:1 to the internal stars_balance
    ledger. "starstopup:menu" shows package buttons, "starstopup:buy:<amount>"
    sends the actual invoice."""
    query = update.callback_query
    await query.answer()

    telegram_user_id = _effective_telegram_user_id(update)
    if telegram_user_id is None:
        await query.edit_message_text("⚠️ 사용자 정보를 확인할 수 없습니다.")
        return

    async with async_session_maker() as db:
        tenant = await bot_account_service.get_referral_info(db, telegram_user_id)

    if tenant is None:
        await query.edit_message_text(
            "⭐ Stars 충전은 요금제를 시작한 후 이용할 수 있습니다.\n"
            "먼저 무료체험 또는 요금제를 시작해주세요.",
            reply_markup=_back_to_main_keyboard(),
        )
        return

    data = query.data
    if data == "starstopup:menu":
        await query.edit_message_text(
            "⭐ Stars 충전\n\n"
            f"현재 보유: {tenant.stars_balance or 0}⭐\n"
            "충전할 수량을 선택해주세요. 결제는 텔레그램 자체 Stars 결제 화면에서 진행됩니다.",
            reply_markup=_stars_topup_menu_keyboard(),
        )
        return

    # starstopup:buy:<amount>
    try:
        amount = int(data.rsplit(":", 1)[-1])
    except ValueError:
        amount = 0
    if amount not in billing.STARS_TOPUP_PACKAGES:
        await query.edit_message_text("⚠️ 유효하지 않은 충전 옵션입니다.", reply_markup=_back_to_main_keyboard())
        return

    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=f"TeleMon Stars {amount:,}개",
        description=f"TeleMon 내 결제(부가기능 구매 등)에 사용할 수 있는 Stars {amount:,}개를 충전합니다.",
        payload=f"{_STARS_TOPUP_PAYLOAD_PREFIX}{amount}",
        currency="XTR",
        prices=[LabeledPrice(f"{amount:,} Stars", amount)],
        provider_token="",
    )
    await query.edit_message_text(
        f"🧾 {amount:,}⭐ 결제 요청을 보냈습니다. 아래 메시지에서 결제를 완료해주세요.",
        reply_markup=_back_to_main_keyboard(),
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer Telegram's pre-checkout query — required within 10s of the user
    tapping pay, or the payment sheet shows an error. Re-validates the payload
    and tenant here since this is the last checkpoint before real money moves."""
    pcq = update.pre_checkout_query
    payload = pcq.invoice_payload or ""

    if not payload.startswith(_STARS_TOPUP_PAYLOAD_PREFIX):
        await pcq.answer(ok=False, error_message="알 수 없는 결제 요청입니다.")
        return

    try:
        amount = int(payload[len(_STARS_TOPUP_PAYLOAD_PREFIX):])
    except ValueError:
        amount = 0

    if amount not in billing.STARS_TOPUP_PACKAGES or pcq.total_amount != amount:
        await pcq.answer(ok=False, error_message="유효하지 않은 결제 금액입니다.")
        return

    async with async_session_maker() as db:
        tenant = await bot_account_service.get_referral_info(db, pcq.from_user.id)
    if tenant is None:
        await pcq.answer(ok=False, error_message="계정을 확인할 수 없습니다. 먼저 요금제를 시작해주세요.")
        return

    await pcq.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Telegram's successful_payment message — the money has already
    moved at this point, this just credits stars_balance (idempotently, see
    billing.credit_stars_from_telegram_payment)."""
    payment = update.message.successful_payment
    payload = payment.invoice_payload or ""
    if not payload.startswith(_STARS_TOPUP_PAYLOAD_PREFIX):
        return

    telegram_user_id = _effective_telegram_user_id(update)
    if telegram_user_id is None:
        return

    async with async_session_maker() as db:
        tenant = await bot_account_service.get_referral_info(db, telegram_user_id)
    if tenant is None:
        logger.error("stars_topup_no_tenant", telegram_user_id=telegram_user_id, charge_id=payment.telegram_payment_charge_id)
        return

    result = await billing.credit_stars_from_telegram_payment(
        tenant.id, payment.total_amount, payment.telegram_payment_charge_id
    )
    if not result.get("success"):
        logger.error("stars_topup_credit_failed", tenant_id=tenant.id, error=result.get("error"))
        return

    balance = result.get("stars_balance", (tenant.stars_balance or 0) + payment.total_amount)
    await update.message.reply_text(
        f"✅ {payment.total_amount:,}⭐ 충전이 완료되었습니다! 현재 보유: {balance:,}⭐",
        reply_markup=_back_to_main_keyboard(),
    )


async def support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle "🆘 고객센터" — static contact info."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        f"🆘 고객센터\n\n문의사항은 {settings.telegram_support_username}(으)로 연락해주세요.",
        reply_markup=_back_to_main_keyboard(),
    )


async def notice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle "📢 공지" — a single config-driven announcement (no admin UI yet)."""
    query = update.callback_query
    await query.answer()
    text = settings.bot_announcement_text or "등록된 공지가 없습니다."
    await query.edit_message_text(f"📢 공지\n\n{text}", reply_markup=_back_to_main_keyboard())


async def aichat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle "🤖 AI Chat" — enters free-text chat mode with the DeepSeek assistant.

    Eligibility (linked + active sub/trial) is the same gate bot_api_key_service
    uses; only after that does this add the user to _active_ai_chat_users so
    ai_chat_text_handler starts routing their free text to DeepSeek.
    """
    query = update.callback_query
    await query.answer()

    telegram_user_id = _effective_telegram_user_id(update)
    if telegram_user_id is None:
        await query.edit_message_text("⚠️ 사용자 정보를 확인할 수 없습니다.")
        return

    async with async_session_maker() as db:
        snapshot = await bot_account_service.get_account_snapshot(db, telegram_user_id)

    if not snapshot.linked:
        await query.edit_message_text(
            "🤖 AI Chat은 TeleMon 계정 연동 후 이용할 수 있습니다.\n"
            "'💰 결제(업그레이드)' 메뉴에서 요금제를 구매하거나 무료체험을 시작해주세요.",
            reply_markup=_back_to_main_keyboard(),
        )
        return

    _active_ai_chat_users.add(telegram_user_id)
    await query.edit_message_text(
        "💬 AI Chat을 시작합니다! 편하게 메시지를 보내보세요.\n"
        "종료하려면 아래 '◀ 메인 메뉴' 버튼을 눌러주세요.",
        reply_markup=_back_to_main_keyboard(),
    )


async def ai_chat_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-text handler for the "AI Chat" flow. Only acts for users who entered
    that mode via aichat_callback — every other free-text message is ignored,
    exactly like before this feature existed (no regression for other flows)."""
    telegram_user_id = _effective_telegram_user_id(update)
    if telegram_user_id is None or telegram_user_id not in _active_ai_chat_users:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    async with async_session_maker() as db:
        result = await ai_chat_service.send_message(db, telegram_user_id, update.message.text or "")

    if result.status == "ok":
        await update.message.reply_text(result.reply)
        return

    if result.status == "quota_exceeded":
        await update.message.reply_text(f"📊 {result.detail}", reply_markup=_pay_menu_keyboard())
        return

    prefix = {
        "not_linked": "🔗",
        "not_eligible": "🚫",
        "rate_limited": "⏳",
        "too_long": "✂️",
        "server_error": "⚠️",
    }.get(result.status, "⚠️")
    await update.message.reply_text(f"{prefix} {result.detail}")


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle "◀ 메인 메뉴" — return to the top-level menu from any submenu.

    Also exits AI Chat mode if the user was in it, so their free text stops
    being routed to DeepSeek once they've navigated away.
    """
    query = update.callback_query
    await query.answer()

    telegram_user_id = _effective_telegram_user_id(update)
    if telegram_user_id is not None:
        _active_ai_chat_users.discard(telegram_user_id)

    await query.edit_message_text("안녕하세요! 아래 메뉴에서 원하는 기능을 선택해주세요.", reply_markup=_main_menu_keyboard())


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /start (bare) and the deep-link form /start <token> used by the
    free-trial official-channel verification flow (see app/api/telegram_verify.py).

    This is the one place in the whole flow where a Telegram user id is obtained —
    it comes straight from Telegram's own Update object for this bot's polling
    connection, so it cannot be forged by anything the frontend sends us.
    """
    if not context.args:
        await update.message.reply_text(
            "안녕하세요! TeleMon 봇입니다.\n아래 메뉴에서 원하는 기능을 선택해주세요.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    token = context.args[0]
    telegram_user_id = update.effective_user.id if update.effective_user else None
    if telegram_user_id is None:
        return

    if token.startswith("ref_"):
        purchase_service.set_pending_referral(telegram_user_id, token[len("ref_"):])
        await update.message.reply_text(
            "🎁 추천 링크로 오셨네요! 요금제를 시작하시면 추천인에게 보상이 지급됩니다.\n\n"
            "안녕하세요! TeleMon 봇입니다.\n아래 메뉴에서 원하는 기능을 선택해주세요.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    async with async_session_maker() as db:
        linked = await verification_crud.link_telegram_user(db, token, telegram_user_id)

    if linked:
        await update.message.reply_text(
            "✅ 확인되었습니다! 이제 브라우저로 돌아가 채널 가입 여부 확인을 계속 진행해주세요."
        )
    else:
        await update.message.reply_text(
            "⚠️ 인증 링크가 만료되었거나 유효하지 않습니다. 웹사이트에서 다시 시도해주세요."
        )


async def start_bot() -> None:
    """No-op if TELEGRAM_BOT_TOKEN isn't set — the bot is an optional remote-control
    convenience on top of the dashboard's own toggle, not a hard dependency."""
    global _application
    if not settings.telegram_bot_token:
        logger.info("telegram_bot_skipped", reason="no_token")
        return

    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("autoreply", autoreply_command))
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^autoreply"))
    application.add_handler(CallbackQueryHandler(apikey_callback, pattern=r"^apikey:"))
    application.add_handler(CallbackQueryHandler(plan_callback, pattern=r"^plan:"))
    application.add_handler(CallbackQueryHandler(account_status_callback, pattern=r"^account:"))
    application.add_handler(CallbackQueryHandler(pay_callback, pattern=r"^pay:"))
    application.add_handler(CallbackQueryHandler(renew_callback, pattern=r"^renew:"))
    application.add_handler(CallbackQueryHandler(purchase_history_callback, pattern=r"^purchase:"))
    application.add_handler(CallbackQueryHandler(checkin_callback, pattern=r"^checkin:"))
    application.add_handler(CallbackQueryHandler(referral_callback, pattern=r"^referral:"))
    application.add_handler(CallbackQueryHandler(starstopup_callback, pattern=r"^starstopup:"))
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    application.add_handler(CallbackQueryHandler(support_callback, pattern=r"^support:"))
    application.add_handler(CallbackQueryHandler(notice_callback, pattern=r"^notice:"))
    application.add_handler(CallbackQueryHandler(aichat_callback, pattern=r"^aichat:"))
    application.add_handler(CallbackQueryHandler(main_menu_callback, pattern=r"^menu:main$"))
    application.add_handler(CommandHandler("start", start_command))
    # No other flow reads free text today, so this is safe to add unconditionally —
    # ai_chat_text_handler itself no-ops for anyone not in _active_ai_chat_users.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_chat_text_handler))

    # Non-blocking startup (vs. the usual Application.run_polling(), which blocks forever)
    # so this can live inside the FastAPI lifespan alongside uvicorn's own event loop.
    await application.initialize()
    await application.start()

    # Gracefully close any stale polling session before starting our own, to avoid
    # 409 Conflict when another instance (e.g. a Render.com deployment that shares the
    # same bot token) is still connected.  This is best-effort: close() may fail due to
    # rate limits or network errors, but polling will still be attempted.
    try:
        await application.bot.close()
        logger.info("telegram_bot_stale_session_closed")
    except Exception as exc:
        logger.warning("telegram_bot_close_skipped", error=str(exc))

    # bootstrap_retries: PTB's default (0) means a single transient failure in the
    # startup bootstrap (e.g. Telegram returning a 500 on the delete_webhook call
    # start_polling() makes internally) aborts start_polling() entirely and leaves
    # the bot never polling for the rest of the container's life — the /start
    # command then silently goes unanswered until the next restart. Retry instead.
    await application.updater.start_polling(bootstrap_retries=3)
    _application = application
    logger.info("telegram_bot_started")


async def stop_bot() -> None:
    global _application
    if _application is None:
        return
    await _application.updater.stop()
    await _application.stop()
    await _application.shutdown()
    _application = None
    logger.info("telegram_bot_stopped")