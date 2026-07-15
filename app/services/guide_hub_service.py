"""Publishes/updates the pinned "이용 가이드 허브" message in the official
TeleMon channel — a fixed message with one button per feature, each linking out
to that feature's guide post in the same channel.

Uses a standalone ``Bot`` instance (see app.services.telegram_membership for the
same pattern) rather than the polling ``Application`` in telegram_bot_service —
publishing a channel message has nothing to do with receiving updates, so it
must not touch the bot's command/callback handlers or its polling lifecycle.
"""

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.crud import guide_hub as guide_hub_crud

logger = get_logger(__name__)

# (button key, label) in display order — 2 per row. Keys are looked up in
# settings.telegram_guide_hub_links; a key with no configured URL is omitted
# from the keyboard instead of producing a dead/error button. Kept 1:1 with the
# guide posts that actually exist in the official channel — add a key here
# only once its matching post exists and its URL is added to
# TELEGRAM_GUIDE_HUB_LINKS_JSON, never the other way around.
GUIDE_HUB_BUTTONS: list[tuple[str, str]] = [
    ("homepage", "🏠 홈페이지"),
    ("official_channel", "📢 공식 채널"),
    ("free_trial", "🎁 3일 무료체험"),
    ("send_and_macro", "📨 메시지 발송 & 답장 매크로"),
    ("auto_reply", "🤖 자동응답"),
    ("group_search", "🔍 그룹 검색 및 참여"),
    ("link_inspector", "🔗 링크 일괄검사"),
]

GUIDE_HUB_TEXT = (
    "📚 TeleMon 이용 가이드\n\n"
    "아래 버튼을 눌러 각 기능별 사용법을 확인하세요."
)


class GuideHubUnavailable(Exception):
    """Raised when the bot isn't configured or the Telegram API call fails."""


def _build_keyboard() -> InlineKeyboardMarkup:
    links = settings.telegram_guide_hub_links
    rows: list[list[InlineKeyboardButton]] = []
    pending: InlineKeyboardButton | None = None
    for key, label in GUIDE_HUB_BUTTONS:
        url = links.get(key)
        if not url:
            continue
        button = InlineKeyboardButton(label, url=url)
        if pending is None:
            pending = button
        else:
            rows.append([pending, button])
            pending = None
    if pending is not None:
        rows.append([pending])
    return InlineKeyboardMarkup(rows)


async def publish_or_update_guide_hub(db: AsyncSession) -> tuple[str, int, bool]:
    """Create the guide hub message on first call; edit it in place on every
    later call. Returns (chat_id, message_id, created).

    Raises GuideHubUnavailable if the bot token isn't configured or the
    Telegram API call fails for any reason (never silently no-ops).
    """
    if not settings.telegram_bot_token:
        raise GuideHubUnavailable("TELEGRAM_BOT_TOKEN이 설정되지 않았습니다.")

    chat_id = settings.telegram_official_channel_id
    if not chat_id:
        raise GuideHubUnavailable("TELEGRAM_OFFICIAL_CHANNEL_ID가 설정되지 않았습니다.")

    keyboard = _build_keyboard()
    bot = Bot(token=settings.telegram_bot_token)
    existing = await guide_hub_crud.get_latest(db)

    if existing is not None:
        try:
            await bot.edit_message_text(
                chat_id=existing.chat_id,
                message_id=existing.message_id,
                text=GUIDE_HUB_TEXT,
                reply_markup=keyboard,
            )
            logger.info("guide_hub_updated", chat_id=existing.chat_id, message_id=existing.message_id)
            await guide_hub_crud.upsert(db, existing.chat_id, existing.message_id)
            return existing.chat_id, existing.message_id, False
        except TelegramError as exc:
            # Telegram rejects a no-op edit (identical text + keyboard) with this
            # exact "Bad Request" message — that's a successful idempotent update,
            # not a failure, so it must not fall through to posting a duplicate.
            if "message is not modified" in str(exc).lower():
                logger.info("guide_hub_update_noop", chat_id=existing.chat_id, message_id=existing.message_id)
                await guide_hub_crud.upsert(db, existing.chat_id, existing.message_id)
                return existing.chat_id, existing.message_id, False
            # Otherwise the message may have been deleted/unpinned out-of-band —
            # fall back to posting a fresh one rather than failing the whole call.
            logger.warning("guide_hub_edit_failed_posting_new", error=str(exc))

    try:
        message = await bot.send_message(chat_id=chat_id, text=GUIDE_HUB_TEXT, reply_markup=keyboard)
        await bot.pin_chat_message(chat_id=chat_id, message_id=message.message_id, disable_notification=True)
    except TelegramError as exc:
        logger.error("guide_hub_publish_failed", error=str(exc))
        raise GuideHubUnavailable(str(exc)) from exc

    await guide_hub_crud.upsert(db, str(chat_id), message.message_id)
    logger.info("guide_hub_published", chat_id=chat_id, message_id=message.message_id)
    return str(chat_id), message.message_id, True
