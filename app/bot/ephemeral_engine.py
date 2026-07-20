"""
Ephemeral Messages Engine — Bot API 10.2+ (July 2026) Ephemeral Messages.

그룹 채팅 내에서 특정 사용자에게만 보이는 개인화 메시지를 전송/관리합니다.

용도:
1. Guest Mode 응답 후 개인화된 추가 정보 전송
2. 그룹 신규 가입자 환영 시퀀스
3. 사용량 기반 전환 유도 (프리미엄 업그레이드 제안)

이 모듈은 Telethon을 전혀 사용하지 않으며, Bot API 전용입니다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .telegram_api import TelegramBotClient

logger = logging.getLogger(__name__)


class EphemeralEngine:
    """Ephemeral 메시지 전송 및 관리.

    sendMessage() 에 receiver_user_id 파라미터를 추가하여
    그룹 내 특정 사용자에게만 보이는 메시지를 전송합니다.
    (다른 그룹 멤버에게는 보이지 않음)
    """

    def __init__(self, client: TelegramBotClient) -> None:
        self._client = client
        # user_id(str) -> list of ephemeral message metadata
        self._tracked: dict[str, list[dict[str, Any]]] = {}

    # ── Public API ─────────────────────────────────────────────────

    async def send_personalized(
        self,
        chat_id: int | str,
        user_id: int,
        text: str,
        parse_mode: str | None = "Markdown",
        button_label: str | None = None,
        button_url: str | None = None,
    ) -> dict[str, Any] | None:
        """그룹 내 특정 사용자에게만 개인화 메시지 전송.

        다른 그룹 멤버에게는 이 메시지가 보이지 않습니다.
        """
        reply_markup = None
        if button_label and button_url:
            reply_markup = {
                "inline_keyboard": [[
                    {"text": button_label, "url": button_url}
                ]]
            }

        try:
            result = await self._client.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                receiver_user_id=user_id,
            )
            self._track(user_id, chat_id, result)
            return result
        except Exception as e:
            logger.warning(
                "[ephemeral] send to user %s in chat %s failed: %s",
                user_id, chat_id, e,
            )
            return None

    async def send_welcome(
        self,
        chat_id: int | str,
        user_id: int,
        name: str = "",
    ) -> None:
        """그룹 신규 가입자에게 환영 Ephemeral 메시지 전송."""
        greeting = (
            f"👋 {name}님, 환영합니다!\n\n"
            if name else "👋 환영합니다!\n\n"
        )
        text = (
            f"{greeting}"
            f"이 그룹의 AI 비서 **TeleMon**이 도와드립니다.\n"
            f"저를 @멘션하면 번역, 요약, 날씨 정보를 무료로 이용할 수 있어요!"
        )
        await self.send_personalized(
            chat_id, user_id, text,
            button_label="🚀 TeleMon 시작하기",
            button_url="https://telemon.online",
        )

    async def send_conversion_reminder(
        self,
        chat_id: int | str,
        user_id: int,
        guest_uses: int = 0,
    ) -> None:
        """Guest 사용량이 일정 수준일 때 프리미엄 전환 유도."""
        if guest_uses < 5:
            return

        text = (
            f"🎯 TeleMon을 {guest_uses}번 사용하셨네요!\n\n"
            f"**프리미엄으로 업그레이드하면:**\n"
            f"• 무제한 AI 호출\n"
            f"• 고급 번역/요약\n"
            f"• 우선 처리\n"
            f"를 이용할 수 있어요!"
        )
        await self.send_personalized(
            chat_id, user_id, text,
            button_label="🚀 프리미엄 시작",
            button_url="https://telemon.online/pricing",
        )

    async def clear_user_messages(self, user_id: str) -> int:
        """특정 사용자의 모든 추적 Ephemeral 메시지 삭제."""
        messages = self._tracked.pop(str(user_id), [])
        deleted = 0
        for msg in messages:
            try:
                await self._client.delete_ephemeral_message(
                    msg["chat_id"],
                    msg["ephemeral_message_id"],
                )
                deleted += 1
            except Exception as e:
                logger.warning(
                    "[ephemeral] delete failed: chat=%s, eid=%s: %s",
                    msg["chat_id"], msg["ephemeral_message_id"], e,
                )
        return deleted

    def get_stats(self) -> dict[str, Any]:
        """엔진 상태 스냅샷."""
        total_tracked = sum(len(msgs) for msgs in self._tracked.values())
        return {
            "tracked_users": len(self._tracked),
            "tracked_messages": total_tracked,
        }

    # ── Internal ───────────────────────────────────────────────────

    def _track(
        self, user_id: int, chat_id: int | str, result: dict[str, Any]
    ) -> None:
        """전송된 Ephemeral 메시지 추적 (추후 삭제용)."""
        message = result.get("message", result)
        ephemeral_id = message.get("ephemeral_message_id")
        if not ephemeral_id:
            return

        key = str(user_id)
        if key not in self._tracked:
            self._tracked[key] = []

        self._tracked[key].append({
            "ephemeral_message_id": ephemeral_id,
            "chat_id": chat_id,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "text_preview": (message.get("text") or "")[:60],
        })

        # 메모리 누수 방지
        if len(self._tracked[key]) > 50:
            self._tracked[key] = self._tracked[key][-50:]
