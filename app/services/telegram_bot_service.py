from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.config import settings
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import telegram_verification as verification_crud
from app.database import async_session_maker
from app.services.auto_reply_service import AccountNotAuthenticatedError, disable_auto_reply, enable_auto_reply

logger = get_logger(__name__)

_application: Application | None = None


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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /start (bare) and the deep-link form /start <token> used by the
    free-trial official-channel verification flow (see app/api/telegram_verify.py).

    This is the one place in the whole flow where a Telegram user id is obtained —
    it comes straight from Telegram's own Update object for this bot's polling
    connection, so it cannot be forged by anything the frontend sends us.
    """
    if not context.args:
        await update.message.reply_text("안녕하세요! TeleMon 봇입니다.")
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
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^autoreply:"))
    application.add_handler(CommandHandler("start", start_command))

    # Non-blocking startup (vs. the usual Application.run_polling(), which blocks forever)
    # so this can live inside the FastAPI lifespan alongside uvicorn's own event loop.
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
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
