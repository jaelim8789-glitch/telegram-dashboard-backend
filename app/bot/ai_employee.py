"""
AI Employee — 그룹 채팅에서 @TeleMonBot 멘션을 처리하는 엔진.

GuestEngine과 동일한 decide_action()을 재사용하지만, 실행은
answerGuestQuery 대신 sendMessage로 수행합니다.

아키텍처:
  AiEmployee.process_group_message() 
    → GuestEngine.decide_action()  ← 판단 로직 재사용
    → AiEmployee._execute_for_group() ← sendMessage 실행

style_profile_id 를 지원하여 그룹별 응답 스타일을 적용할 수 있습니다.
DB 기반 설정 (ai_group_style_profiles)과 예약 발송
(ai_scheduled_messages)을 지원합니다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from . import db as bot_db
from .guest_engine import Decision, GuestEngine, RequestContext, _BOT_MENTION_PREFIXES

if TYPE_CHECKING:
    from .telegram_api import TelegramBotClient

logger = logging.getLogger(__name__)


# ── AiEmployee ──────────────────────────────────────────────────────


class AiEmployee:
    """그룹 채팅 AI 어시스턴트.

    GuestEngine의 decide_action()을 재사용하여 명령어 파싱과 핸들러
    디스패치를 수행하고, 실행은 sendMessage()로 합니다.

    Args:
        client: TelegramBotClient 인스턴스.
        guest_engine: 명령어 핸들러와 파싱 로직을 제공할 GuestEngine.
    """

    def __init__(self, client: TelegramBotClient, guest_engine: GuestEngine) -> None:
        self._client = client
        self._guest = guest_engine
        self._bg_task: asyncio.Task[None] | None = None

    def start_background_scheduler(self) -> None:
        """Start the background scheduler for pending scheduled messages."""
        if self._bg_task is None or self._bg_task.done():
            self._bg_task = asyncio.create_task(
                self._scheduler_loop(), name="ai_employee_scheduler"
            )
            logger.info("[ai_employee] background scheduler started")

    def stop_background_scheduler(self) -> None:
        """Stop the background scheduler."""
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
            self._bg_task = None
            logger.info("[ai_employee] background scheduler stopped")

    # ── Public API ─────────────────────────────────────────────────

    async def process_group_message(self, update: dict[str, Any]) -> None:
        """그룹 메시지에서 @봇 멘션을 감지하고 처리.

        update는 Telegram Bot API의 message 객체를 포함한 dict입니다.
        GuestEngine.decide_action()을 호출한 후 sendMessage()로 실행합니다.
        """
        message = update.get("message", {})
        text = message.get("text", "").strip()
        chat_id = message.get("chat", {}).get("id")
        user_id = message.get("from", {}).get("id")

        if not text or not chat_id:
            return

        # 1. 봇 멘션 확인
        if not self._is_bot_mentioned(text):
            return

        # 2. 멘션 제거
        clean_text = self._strip_bot_mention(text)
        if not clean_text:
            clean_text = "도움말"

        # 3. 컨텍스트 생성 (DB 기반 style_profile_id 포함)
        context = RequestContext(
            text=clean_text,
            chat_id=chat_id,
            user_id=str(user_id or 0),
            style_profile_id=self._get_style_profile(chat_id),
            available_actions=self._get_available_actions(chat_id),
        )

        # 4. GuestEngine의 decide_action 재사용 (순수 판단)
        decision = await self._guest.decide_action(context)

        # 5. sendMessage로 실행
        await self._execute_for_group(decision, chat_id)

        logger.info(
            "[ai_employee] group %s | user %s | action=%s",
            chat_id, user_id, decision.action,
        )

    # ── 예약 발송 ────────────────────────────────────────────────────

    async def cancel_scheduled_message(self, msg_id: str) -> bool:
        """예약된 메시지를 취소합니다.

        Args:
            msg_id: 취소할 메시지 ID.

        Returns:
            취소 성공 여부 (이미 발송되었거나 없는 ID면 False).
        """
        result = bot_db.cancel_scheduled_message(msg_id)
        if result:
            logger.info("[ai_employee] scheduled message %s cancelled", msg_id)
        else:
            logger.warning("[ai_employee] failed to cancel message %s (already sent or not found)", msg_id)
        return result

    async def schedule_message(
        self,
        chat_id: int,
        text: str,
        delay_seconds: int,
        parse_mode: str = "Markdown",
    ) -> str:
        """메시지를 예약 발송합니다.

        지정된 delay_seconds 후에 메시지를 전송합니다.
        DB에 저장되므로 서버 재시작 후에도 발송됩니다.

        Returns:
            예약 메시지 ID.
        """
        # 실제 전송 시간 계산
        actual_send_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()

        msg_id = bot_db.insert_scheduled_message(
            chat_id=chat_id,
            text=text,
            send_at=actual_send_at,
            parse_mode=parse_mode,
        )

        logger.info(
            "[ai_employee] message %s scheduled for chat %s in %ds",
            msg_id, chat_id, delay_seconds,
        )
        return msg_id

    async def _scheduler_loop(self) -> None:
        """백그라운드 스케줄러 루프.

        주기적으로 DB에서 발송 대기 중인 메시지를 확인하고 전송합니다.
        """
        while True:
            try:
                pending = bot_db.get_pending_scheduled_messages()
                for msg in pending:
                    try:
                        await self._client.send_message(
                            msg["chat_id"],
                            msg["text"],
                            parse_mode=msg.get("parse_mode", "Markdown"),
                        )
                        bot_db.mark_scheduled_message_sent(msg["id"])
                        logger.info(
                            "[ai_employee] scheduled msg %s sent to chat %s",
                            msg["id"], msg["chat_id"],
                        )
                    except Exception as e:
                        bot_db.mark_scheduled_message_failed(msg["id"], str(e))
                        logger.error(
                            "[ai_employee] scheduled msg %s failed: %s",
                            msg["id"], e,
                        )
                await asyncio.sleep(10)  # 10초마다 확인
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[ai_employee] scheduler loop error")
                await asyncio.sleep(30)

    # ── 봇 멘션 감지 / 제거 ─────────────────────────────────────────

    @staticmethod
    def _is_bot_mentioned(text: str) -> bool:
        """텍스트가 @TeleMonBot 멘션으로 시작하는지 확인."""
        for prefix in _BOT_MENTION_PREFIXES:
            if text.lower().startswith(prefix.lower()):
                return True
        return False

    @staticmethod
    def _strip_bot_mention(text: str) -> str:
        """텍스트에서 @TeleMonBot 멘션 접두사 제거."""
        for prefix in _BOT_MENTION_PREFIXES:
            if text.lower().startswith(prefix.lower()):
                return text[len(prefix):].strip()
        return text

    # ── 그룹 설정 조회 (DB 기반) ────────────────────────────────────

    def _get_style_profile(self, chat_id: int) -> str | None:
        """그룹의 활성 StyleProfile을 DB에서 조회.

        ai_group_style_profiles 테이블에서 해당 그룹의 설정을 읽습니다.
        설정이 없으면 None 반환 (Guest 모드와 동일).
        """
        try:
            profile = bot_db.get_group_style_profile(chat_id)
            if profile:
                return profile.get("style_profile_id") or None
        except Exception:
            logger.debug("[ai_employee] failed to load style profile for %s", chat_id)
        return None

    def _get_available_actions(self, chat_id: int) -> list[str]:
        """그룹에서 사용 가능한 액션 목록을 DB에서 조회.

        ai_group_style_profiles 테이블에 설정된 액션 목록을 사용합니다.
        설정이 없으면 기본 액션 목록을 반환합니다.
        """
        try:
            profile = bot_db.get_group_style_profile(chat_id)
            if profile and profile.get("available_actions"):
                return profile["available_actions"]
        except Exception:
            logger.debug("[ai_employee] failed to load available actions for %s", chat_id)
        return [
            "번역", "translate",
            "요약", "summarize",
            "날씨", "weather",
            "뉴스", "news",
            "도움말", "help",
        ]

    # ── 그룹 메시지 전송 ────────────────────────────────────────────

    async def _execute_for_group(self, decision: Decision, chat_id: int) -> None:
        """Decision을 sendMessage로 실행.

        GuestEngine의 answerGuestQuery와 달리 일반 메시지로 발송합니다.
        rate_limited 결정은 그룹에서 무시됩니다 (그룹 사용자는
        게스트 일일 한도의 영향을 받지 않음).
        """
        if decision.action in ("noop", "rate_limited"):
            logger.debug(
                "[ai_employee] skipping action=%s for chat %s",
                decision.action, chat_id,
            )
            return

        await self._client.send_message(
            chat_id,
            decision.text,
            parse_mode=decision.parse_mode,
            # 그룹 메시지에서는 프로모션 키보드 제거
            reply_markup=None,
        )
