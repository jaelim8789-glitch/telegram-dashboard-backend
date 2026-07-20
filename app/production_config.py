"""Minimal production_config adapter for telegram-dashboard-backend.

Wraps app.config.settings to provide the legacy get_config() interface
used by ported bot routers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import settings as _settings


@dataclass
class TelegramBotConfig:
    bot_token: str = _settings.telegram_bot_token or ""
    webhook_url: str = getattr(_settings, "telegram_webhook_url", "") or ""
    webhook_secret: str = getattr(_settings, "telegram_webhook_secret", "") or ""
    admin_chat_ids: list[str] = field(default_factory=list)
    channel_id: str = getattr(_settings, "telegram_official_channel_id", "") or ""


@dataclass
class Config:
    telegram_bot: TelegramBotConfig = field(default_factory=TelegramBotConfig)


_config = Config()


def get_config() -> Config:
    return _config
