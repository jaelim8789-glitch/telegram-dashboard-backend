"""Best-effort push messages to a single Telegram chat — e.g. "your USDT
payment cleared" pushed from the scheduled watcher job.

Uses a standalone ``Bot`` instance rather than the polling ``Application`` in
telegram_bot_service (same reasoning as app.services.guide_hub_service):
sending an unsolicited message has nothing to do with receiving updates, so it
must not touch the bot's command/callback handlers or its polling lifecycle.

Never raises — a failed push must never break the payment-processing
transaction that triggered it.
"""

from telegram import Bot
from telegram.error import TelegramError

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


async def send_telegram_message(chat_id: int, text: str, parse_mode: str | None = None) -> bool:
    """Send a one-off message to ``chat_id``. Returns True on success, False on
    any failure (bot not configured, user blocked the bot, network error, ...)."""
    if not settings.telegram_bot_token:
        logger.info("telegram_notify_skipped", reason="no_token", chat_id=chat_id)
        return False

    try:
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
        return True
    except TelegramError as exc:
        logger.warning("telegram_notify_failed", chat_id=chat_id, error=str(exc))
        return False
    except Exception as exc:
        logger.error("telegram_notify_unexpected_error", chat_id=chat_id, error=str(exc))
        return False
