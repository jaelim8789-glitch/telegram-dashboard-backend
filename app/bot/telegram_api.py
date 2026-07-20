"""
Thin async wrapper around the Telegram Bot API (HTTPS, not MTProto/Telethon).

Only the handful of methods the bot module needs are implemented.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramAPIError(Exception):
    def __init__(self, method: str, description: str, error_code: int | None = None) -> None:
        super().__init__(f"{method} failed ({error_code}): {description}")
        self.method = method
        self.description = description
        self.error_code = error_code


class TelegramBotClient:
    """Stateless-ish client — one instance per bot token."""

    def __init__(self, bot_token: str, timeout: float = 10.0) -> None:
        self._token = bot_token
        self._timeout = timeout

    async def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = _API_BASE.format(token=self._token, method=method)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=payload or {})
        data = resp.json()
        if not data.get("ok"):
            raise TelegramAPIError(
                method,
                data.get("description", "unknown error"),
                data.get("error_code"),
            )
        return data["result"]

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
        receiver_user_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a message, optionally ephemeral (visible only to receiver_user_id).

        When *receiver_user_id* is set, the message is an Ephemeral Message
        (Bot API 10.2+, July 2026) — only the target user and the bot see it.
        """
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if receiver_user_id is not None:
            payload["receiver_user_id"] = receiver_user_id
        return await self._call("sendMessage", payload)

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None, show_alert: bool = False
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": show_alert}
        if text:
            payload["text"] = text
        return await self._call("answerCallbackQuery", payload)

    async def get_chat_member(self, chat_id: int | str, user_id: int) -> dict[str, Any]:
        return await self._call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    # ── Guest Mode (Bot API 10.0+, May 2026) ─────────────────────────

    async def answer_guest_query(
        self,
        guest_query_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Respond to a guest query (@mention in a group where the bot is not a member).

        The response is ephemeral — only the user who @mentioned the bot sees it.
        """
        payload: dict[str, Any] = {
            "guest_query_id": guest_query_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("answerGuestQuery", payload)

    # ── Telegram Stars Payments ───────────────────────────────────────

    async def send_invoice(
        self,
        chat_id: int | str,
        title: str,
        description: str,
        payload: str,
        currency: str,
        prices: list[dict[str, Any]],
        provider_token: str = "",
        max_tip_amount: int | None = None,
    ) -> dict[str, Any]:
        """Send a Telegram Stars invoice to a user.

        For digital goods, provider_token must be empty string.
        currency must be "XTR" for Telegram Stars.
        """
        api_payload: dict[str, Any] = {
            "chat_id": chat_id,
            "title": title,
            "description": description,
            "payload": payload,
            "provider_token": provider_token,
            "currency": currency,
            "prices": prices,
        }
        if max_tip_amount is not None:
            api_payload["max_tip_amount"] = max_tip_amount
        return await self._call("sendInvoice", api_payload)

    async def answer_pre_checkout_query(
        self,
        pre_checkout_query_id: str,
        ok: bool,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        """Respond to a pre_checkout_query."""
        api_payload: dict[str, Any] = {
            "pre_checkout_query_id": pre_checkout_query_id,
            "ok": ok,
        }
        if error_message:
            api_payload["error_message"] = error_message
        return await self._call("answerPreCheckoutQuery", api_payload)

    # ── Ephemeral Message Management (Bot API 10.2+, July 2026) ───────

    async def edit_ephemeral_message_text(
        self,
        chat_id: int | str,
        ephemeral_message_id: int,
        text: str,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        """Edit an ephemeral message's text."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "ephemeral_message_id": ephemeral_message_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return await self._call("editEphemeralMessageText", payload)

    async def delete_ephemeral_message(
        self,
        chat_id: int | str,
        ephemeral_message_id: int,
    ) -> dict[str, Any]:
        """Delete an ephemeral message."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "ephemeral_message_id": ephemeral_message_id,
        }
        return await self._call("deleteEphemeralMessage", payload)

    # ── Webhook Management ────────────────────────────────────────────

    async def set_webhook(
        self,
        url: str,
        secret_token: str | None = None,
        allowed_updates: list[str] | None = None,
    ) -> dict[str, Any]:
        """Set webhook with configurable allowed_updates.

        Default (when *allowed_updates* is None): legacy-compatible
        [\"message\", \"callback_query\"]. Pass an explicit list to include
        \"guest_message\" or \"ephemeral_message\" after enabling them via @BotFather.
        """
        if allowed_updates is None:
            allowed_updates = ["message", "callback_query"]
        payload: dict[str, Any] = {"url": url, "allowed_updates": allowed_updates}
        if secret_token:
            payload["secret_token"] = secret_token
        return await self._call("setWebhook", payload)

    async def delete_webhook(self) -> dict[str, Any]:
        return await self._call("deleteWebhook", {})

    async def get_webhook_info(self) -> dict[str, Any]:
        return await self._call("getWebhookInfo", {})


_CHANNEL_MEMBER_STATUSES = {"creator", "administrator", "member"}


def is_channel_member_status(status: str) -> bool:
    return status in _CHANNEL_MEMBER_STATUSES
