from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.config import settings
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import telegram_verification as verification_crud
from app.database import async_session_maker
from app.services.auto_reply_service import AccountNotAuthenticatedError, disable_auto_reply, enable_auto_reply
from app.services.bot_api_key_service import handle_self_service_api_key

logger = get_logger(__name__)

_application: Application | None = None


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    """Top-level bot menu — includes the self-service API key button."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔑 API 키 받기", callback_data="apikey:get")],
            [InlineKeyboardButton("🤖 자동 응답 관리", callback_data="autoreply_menu")],
        ]
    )


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
    application.add_handler(CommandHandler("start", start_command))

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