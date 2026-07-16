"""Canonical mapping between a Telegram user id and the phone-equivalent
identifier used as ``User.phone`` / ``Tenant.phone`` for Telegram-only accounts
(no real phone number on file). Single source of truth so every bot-facing
service agrees on the exact format.
"""

import re

_TG_IDENTIFIER_RE = re.compile(r"^tg_(\d+)$")


def tg_identifier(telegram_user_id: int) -> str:
    """Canonical phone-equivalent identifier for a Telegram-only user."""
    return f"tg_{telegram_user_id}"


def parse_tg_identifier(identifier: str) -> int | None:
    """Reverse of ``tg_identifier`` — returns the Telegram user id if
    ``identifier`` is a ``tg_<digits>`` string, else ``None``.

    For a private bot chat, the Telegram chat id equals the user id, so this
    doubles as the chat id to push a message to.
    """
    match = _TG_IDENTIFIER_RE.match(identifier)
    if match is None:
        return None
    return int(match.group(1))
