"""Server-side verification that a Telegram user is currently a member of the
official TeleMon channel — the only trusted signal for the free-trial signup gate.

Never trust a frontend claim of membership. This module is the sole place that
decides "verified" vs "not verified", and it fails closed: any error talking to the
Telegram Bot API (missing config, network failure, bot not an admin of the channel,
Telegram API error) is treated as NOT verified, never as an implicit pass.
"""

from telegram import Bot
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Allow-list, not a deny-list — anything not explicitly here (left, banned/kicked,
# restricted, or any future status Telegram adds) is rejected by default.
_ACTIVE_MEMBER_STATUSES = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}


class MembershipCheckUnavailable(Exception):
    """Raised when the Telegram API couldn't be reached or isn't configured —
    callers must treat this the same as "not a member" (fail closed)."""


async def is_channel_member(telegram_user_id: int) -> bool:
    if not settings.telegram_bot_token or not settings.telegram_official_channel_id:
        logger.warning("telegram_membership_check_unconfigured")
        raise MembershipCheckUnavailable("official channel verification is not configured")

    bot = Bot(token=settings.telegram_bot_token)
    try:
        member = await bot.get_chat_member(
            chat_id=settings.telegram_official_channel_id,
            user_id=telegram_user_id,
        )
    except TelegramError as exc:
        logger.warning("telegram_membership_check_failed", error=str(exc))
        raise MembershipCheckUnavailable(str(exc)) from exc

    is_active = member.status in _ACTIVE_MEMBER_STATUSES
    logger.info("telegram_membership_checked", status=str(member.status), accepted=is_active)
    return is_active
