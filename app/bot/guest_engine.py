"""
Guest Mode Engine — Bot API 10.0+ (May 2026) Guest Mode 처리.

@TeleMonBot 멘션을 받으면:
1. 컨텍스트 생성 (RequestContext)
2. decide_action() 으로 응답 결정 (순수 판단 로직)
3. execute_decision() 으로 Telegram Bot API 전송
4. 방문 기록 DB 저장

아키텍처:
  decide_action(RequestContext) -> Decision    ← 판단과 실행의 분리
  execute_decision(Decision, guest_query_id)   ← Telegram API 호출
  _call_ai(prompt, context) -> str             ← AI 호출 단일 진입점

Guest Bot은 Decision을 answerGuestQuery로 실행합니다.
AI Employee는 같은 decide_action 호출 후 sendMessage/예약발송 등
다른 방식으로 실행할 수 있습니다 — 판단 로직 자체는 재사용.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Coroutine

import httpx

if TYPE_CHECKING:
    from .telegram_api import TelegramBotClient

from . import db as bot_db

logger = logging.getLogger(__name__)

# ── AI 설정 ──────────────────────────────────────────────────────────

_DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# ── 봇 멘션 접두사 (ai_employee.py와 공유) ────────────────────────────

_BOT_MENTION_PREFIXES = ["@TeleMonBot", "@telemonbot", "@telemon_bot", "@TeleMon_Bot"]
_DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
_DEEPSEEK_MODEL = "deepseek-chat"


# ── 컨텍스트 / 결정 자료구조 ─────────────────────────────────────────


@dataclass
class RequestContext:
    """요청 컨텍스트 — 확장 가능한 입력 객체.

    Guest Bot은 style_profile_id=None, 액션 종류 고정으로 채웁니다.
    AI Employee는 그룹의 StyleProfile과 더 넓은 액션 메뉴를 채워넣습니다.

    Attributes:
        text: 핸들러에 전달된 텍스트 (명령어 파싱 후 args 부분).
        chat_id: 요청이 발생한 채팅 ID.
        user_id: 요청한 사용자 ID (문자열).
        style_profile_id: AI 응답 스타일 프로필 ID (Guest는 None).
        available_actions: 사용 가능한 액션 목록.
        command: 파싱된 명령어 이름 (내부 라우팅용).
    """

    text: str
    chat_id: int | None
    user_id: str
    style_profile_id: str | None = None
    available_actions: list[str] | None = None        # ── 내부 라우팅용 ────────────────────────────────────────────────
    command: str = ""

    def __post_init__(self) -> None:
        if self.available_actions is None:
            self.available_actions = [
                "번역", "translate",
                "요약", "summarize",
                "날씨", "weather",
                "뉴스", "news",
                "도움말", "help",
                "시작", "start",
            ]


@dataclass
class Decision:
    """순수 판단 결과 — '무엇을 할지'만 결정, 실행 방법은 포함하지 않음.

    execute_decision()이 action 타입에 따라 적절한 Telegram API를 호출합니다.

    Attributes:
        action: 응답 유형 ("reply" | "rate_limited" | "error" | "noop").
        text: 응답 텍스트.
        parse_mode: Telegram parse_mode (기본 "Markdown").
        reply_markup: 인라인 키보드 등.
    """

    action: str  # "reply" | "rate_limited" | "error" | "noop"
    text: str
    parse_mode: str | None = "Markdown"
    reply_markup: dict[str, Any] | None = None


# ── AI 호출 단일 진입점 ──────────────────────────────────────────────


async def _call_ai(
    prompt: str,
    context: RequestContext | None = None,
    system_prompt: str | None = None,
) -> str:
    """DeepSeek (또는 다른 LLM)을 호출하고 응답 텍스트를 반환.

    이 함수가 이 모듈의 유일한 AI 호출 지점입니다.
    나중에 AI Employee가 추가될 때 이 함수 하나만 교체/확장하면 됩니다.

    현재 구현: DEEPSEEK_API_KEY 환경변수가 설정되어 있으면 실제 API 호출,
    없으면 안내 메시지를 반환합니다.

    Args:
        prompt: 사용자 측 프롬프트 (번역할 텍스트, 요약할 텍스트 등).
        context: 요청 컨텍스트 (로깅용, 선택).
        system_prompt: 시스템 메시지 오버라이드. None이면 기본 프롬프트 사용.
    """
    if not _DEEPSEEK_API_KEY:
        return "⏳ AI 연동 준비 중입니다. 곧 사용할 수 있어요!"

    if system_prompt is None:
        system_prompt = (
            "You are TeleMon AI, a helpful Telegram assistant. "
            "Respond in Korean unless the user wrote in another language. "
            "Keep responses concise and friendly."
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                _DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {_DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _DEEPSEEK_MODEL,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 1024,
                },
            )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception:
        logger.exception("[ai] DeepSeek API call failed")
        return "⚠️ AI 응답 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."


# ── 도움말 텍스트 ───────────────────────────────────────────────────

GUEST_HELP_TEXT = """🤖 **TeleMon AI 비서**

저를 @멘션하면 언제든지 도와드려요:

• `@TeleMonBot 번역 [텍스트]` — 영어↔한국어 번역
• `@TeleMonBot 요약 [텍스트]` — 긴 글 요약
• `@TeleMonBot 날씨 [도시]` — 날씨 정보
• `@TeleMonBot 뉴스 [주제]` — 최신 뉴스 요약
• `@TeleMonBot 도움말` — 이 메시지 표시

💡 **TeleMon의 모든 기능을 사용해보세요**
👉 자동 응답 / 예약 발송 / AI 채팅 / 채널 분석
👉 **telemon.online** 에서 무료로 시작!"""


# ── 공통 프로모션 키보드 ──────────────────────────────────────────────

_PROMO_REPLY_MARKUP: dict[str, Any] = {
    "inline_keyboard": [[
        {"text": "🚀 TeleMon에서 더 많은 기능 사용하기", "url": "https://telemon.online"}
    ]],
}

_LIMIT_REPLY_MARKUP: dict[str, Any] = {
    "inline_keyboard": [[
        {"text": "🚀 TeleMon 시작하기", "url": "https://telemon.online"}
    ]],
}


# ── 핸들러 타입 ─────────────────────────────────────────────────────

GuestHandler = Callable[["RequestContext"], Coroutine[Any, Any, str]]
"""Signature: (context: RequestContext) -> response_text"""


# ── GuestEngine ─────────────────────────────────────────────────────


class GuestEngine:
    """Guest Mode 요청을 처리하는 엔진.

    Thread-safe 하게 설계: _daily_usage 는 int 증가만 하므로
    GIL 아래에서 별도 Lock 없이 안전합니다.
    """

    def __init__(
        self,
        client: TelegramBotClient,
        daily_limit: int = 20,
    ) -> None:
        self._client = client
        self._daily_limit = daily_limit

        # user_id -> today_count (in-memory, 서버 재시작 시 리셋)
        self._daily_usage: dict[str, int] = {}
        # user_id -> per-user limit override (None = use global daily_limit)
        self._user_limits: dict[str, int] = {}
        # 오늘 날짜 캐시 (자정에 자동 리셋)
        self._today = datetime.now(timezone.utc).date()

        # 등록된 명령어 핸들러
        self._commands: dict[str, GuestHandler] = {
            "번역": self._handle_translate,
            "translate": self._handle_translate,
            "요약": self._handle_summarize,
            "summarize": self._handle_summarize,
            "날씨": self._handle_weather,
            "weather": self._handle_weather,
            "뉴스": self._handle_news,
            "news": self._handle_news,
            "help": self._handle_help,
            "도움말": self._handle_help,
            "시작": self._handle_start,
            "start": self._handle_start,
            "등록": self._handle_register_command,
            "register": self._handle_register_command,
            "register_command": self._handle_register_command,
        }
        # DB에서 저장된 커스텀 명령어 로드 (서버 재시저 후 복원)
        self._custom_command_names: set[str] = set()
        self._custom_db_prompts: dict[str, str] = {}  # name -> system_prompt
        self._load_custom_commands_from_db()

        # ————————

    # ── Public API ─────────────────────────────────────────────────

    def add_command(self, name: str, handler: GuestHandler) -> None:
        """Register a new command handler at runtime.

        AiEmployee가 이 메서드를 호출하여 동적으로 새 명령어를 등록할 수 있습니다.
        등록된 명령어는 기존 명령어와 동일하게 _parse_command → _commands에서 찾습니다.

        Args:
            name: 명령어 이름 (예: "맞춤법", "코드리뷰").
            handler: RequestContext를 받아 응답 텍스트를 반환하는 async 함수.
        """
        self._commands[name.lower()] = handler
        logger.info("[guest] custom command registered: '%s'", name)

    @property
    def daily_limit(self) -> int:
        return self._daily_limit

    @daily_limit.setter
    def daily_limit(self, value: int) -> None:
        self._daily_limit = max(1, value)

    @property
    def daily_usage_snapshot(self) -> dict[str, int]:
        """오늘의 사용량 스냅샷 (읽기 전용 복사본)."""
        self._rotate_date()
        return dict(self._daily_usage)

    @property
    def unique_users_today(self) -> int:
        self._rotate_date()
        return len(self._daily_usage)

    @property
    def total_requests_today(self) -> int:
        self._rotate_date()
        return sum(self._daily_usage.values())

    # ── Core handler ───────────────────────────────────────────────

    async def handle_guest_message(self, update: dict[str, Any]) -> None:
        """Process a single guest_message update from Telegram.

        This is the main entry point called by bot/service.py.
        Extracts the update fields, builds context, then delegates to
        decide_action() + execute_decision().
        """
        guest_msg = update.get("guest_message", {})
        guest_query_id = guest_msg.get("guest_query_id", "")
        raw_text = guest_msg.get("text", "").strip()
        chat_id = guest_msg.get("chat_id")
        user_id = guest_msg.get("user_id")
        user_id_str = str(user_id) if user_id else "0"

        if not guest_query_id or not raw_text:
            logger.warning("Guest update missing guest_query_id or text")
            return

        # 1. Build context & decide action (pure judgment)
        context = RequestContext(
            text=raw_text,
            chat_id=chat_id,
            user_id=user_id_str,
        )
        decision = await self.decide_action(context)

        # 2. Execute decision via Telegram Bot API
        await self.execute_decision(decision, guest_query_id)

        # 3. Track usage only for successful replies
        if decision.action == "reply":
            self._rotate_date()
            current_usage = self._daily_usage.get(user_id_str, 0)
            self._daily_usage[user_id_str] = current_usage + 1
            effective_limit = self._user_limits.get(user_id_str, self._daily_limit)
            logger.info(
                "[guest] %s used (usage: %d/%d, action=%s)",
                user_id_str, current_usage + 1, effective_limit, decision.action,
            )

    # ── 판단(Decide) — 순수 로직, API 호출 없음 ──────────────────────

    async def decide_action(self, context: RequestContext) -> Decision:
        """순수 판단 로직 — 컨텍스트를 바탕으로 뭘 할지만 결정.

        이 메서드는 Telegram API를 전혀 호출하지 않습니다.
        반환된 Decision은 execute_decision()으로 실행됩니다.

        AI Employee가 그룹 메시지에 대해 이 메서드를 호출하면
        같은 판단 로직을 재사용할 수 있습니다.
        """
        self._rotate_date()

        # 1. 일일 한도 체크
        current_usage = self._daily_usage.get(context.user_id, 0)
        effective_limit = self._user_limits.get(context.user_id, self._daily_limit)
        if current_usage >= effective_limit:
            logger.info(
                "[guest] user %s hit daily limit (%d)",
                context.user_id, effective_limit,
            )
            return Decision(
                action="rate_limited",
                text=(
                    f"⚠️ 오늘의 무료 사용 한도({effective_limit}회)를 모두 사용했습니다.\n\n"
                    f"🚀 **TeleMon 프리미엄**으로 업그레이드하면 무제한으로 이용할 수 있어요!\n"
                    f"👉 telemon.online"
                ),
                reply_markup=_LIMIT_REPLY_MARKUP,
            )

        # 2. 명령어 파싱
        command, args = self._parse_command(context.text)

        # 3. 적절한 핸들러 찾기
        handler = self._commands.get(command.lower(), self._handle_fallback)

        # 4. 핸들러 전용 컨텍스트 생성 (text = args, command = 파싱된 명령어)
        handler_context = RequestContext(
            text=args,
            chat_id=context.chat_id,
            user_id=context.user_id,
            style_profile_id=context.style_profile_id,
            available_actions=context.available_actions,
            command=command,
        )

        # 5. 응답 생성
        try:
            response_text = await handler(handler_context)
        except Exception:
            logger.exception(
                "[guest] response generation failed for user %s", context.user_id,
            )
            response_text = (
                "⚠️ 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해주세요.\n\n"
                f"💡 telemon.online 에서 더 많은 기능을 이용할 수 있어요!"
            )

        return Decision(
            action="reply",
            text=response_text,
            reply_markup=_PROMO_REPLY_MARKUP,
        )

    # ── 실행(Execute) — Decision을 Telegram API로 전송 ──────────────

    async def execute_decision(self, decision: Decision, guest_query_id: str) -> None:
        """Decision을 Telegram Bot API로 실행.

        현재는 answerGuestQuery()로 응답합니다.
        AI Employee 도입 시 이 메서드를 확장하여 sendMessage/예약발송
        등 다른 실행 방식을 지원할 수 있습니다.
        """
        if decision.action == "noop":
            return

        await self._client.answer_guest_query(
            guest_query_id,
            decision.text,
            parse_mode=decision.parse_mode,
            reply_markup=decision.reply_markup,
        )

    # ── Command parsing ────────────────────────────────────────────

    def _parse_command(self, raw_text: str) -> tuple[str, str]:
        """@멘션 제거 + 첫 단어를 명령어로 분리.

        "@TeleMonBot 번역 Hello World" -> ("번역", "Hello World")
        "번역 Hello World"              -> ("번역", "Hello World")
        "@TeleMonBot"                   -> ("도움말", "")
        """
        text = raw_text.strip()

        # @멘션 접두사 제거 (대소문자 무시)
        for prefix in _BOT_MENTION_PREFIXES:
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].strip()
                break

        if not text:
            return "도움말", ""

        parts = text.split(maxsplit=1)
        command = parts[0].strip()
        args = parts[1].strip() if len(parts) > 1 else ""
        return command, args

    # ── Individual command handlers ────────────────────────────────

    async def _handle_translate(self, context: RequestContext) -> str:
        if not context.text:
            return (
                "📝 **번역 사용법**\n\n"
                f"`@TeleMonBot 번역 [텍스트]`\n\n"
                f"예시: `@TeleMonBot 번역 Hello World`\n"
                f"예시: `@TeleMonBot translate 안녕하세요`"
            )

        prompt = (
            f"Translate the following text.\n"
            f"- If it's Korean, translate to English.\n"
            f"- If it's English, translate to Korean.\n"
            f"- If it's another language, translate to Korean.\n"
            f"Return ONLY the translated text — no explanations, no quotes, no formatting.\n\n"
            f"Text: {context.text}"
        )
        system_prompt = (
            "You are a professional translator. "
            "Your job is to translate text accurately and naturally. "
            "Never add explanations, notes, or formatting — return only the translation."
        )

        translated = await _call_ai(prompt, context, system_prompt=system_prompt)

        # 백틱이 섞여 들어오면 마크다운 코드블록이 깨지므로 안전하게 치환
        safe_translated = translated.replace("`", "'")

        # 표시용 원본 텍스트도 길이 제한
        display_original = context.text[:3000] + "…" if len(context.text) > 3000 else context.text

        # Telegram 메시지 길이 한도(~4096) — 전체 길이를 동적으로 계산
        # 오버헤드 = "🌐 **번역 결과**\n\n```\n"(18) + "\n```\n➡️ ```\n"(13) + "\n```\n\n💡..."(38)
        overhead = 18 + 13 + 38
        budget = 4096 - overhead - len(display_original) - 1  # 1 = 여유
        if budget < 100:
            budget = 100  # 최소 100자 보장
            display_original = display_original[:500]
        safe_translated = safe_translated[:budget] + "…" if len(safe_translated) > budget else safe_translated

        return (
            f"🌐 **번역 결과**\n\n"
            f"```\n{display_original}\n```\n"
            f"➡️ ```\n{safe_translated}\n```\n\n"
            f"💡 TeleMon에서 더 많은 AI 기능을 이용하세요!"
        )

    async def _handle_summarize(self, context: RequestContext) -> str:
        if not context.text:
            return (
                "📝 **요약 사용법**\n\n"
                f"`@TeleMonBot 요약 [긴 텍스트]`\n\n"
                f"예시: `@TeleMonBot 요약 오늘 회의에서는 ...`"
            )
        if len(context.text) < 20:
            return "📋 요약할 텍스트가 너무 짧습니다. 더 긴 텍스트를 입력해주세요."

        prompt = (
            f"Summarize the following text concisely in Korean.\n"
            f"- Extract the key points only.\n"
            f"- Keep the summary under 500 characters.\n"
            f"- Use bullet points for clarity if there are multiple main ideas.\n"
            f"- Do not add opinions or commentary — just summarize.\n\n"
            f"Text: {context.text}"
        )
        system_prompt = (
            "You are a professional summarizer. "
            "Your job is to distill long texts into concise, accurate summaries. "
            "Always respond in Korean. Never add your own opinions."
        )

        summary = await _call_ai(prompt, context, system_prompt=system_prompt)

        # Telegram 메시지 길이 한도(~4096)에 대비 + 마크다운 안전
        safe_summary = summary.replace("`", "'")
        if len(safe_summary) > 3500:
            safe_summary = safe_summary[:3500] + "…"

        return (
            f"📋 **요약 결과**\n\n"
            f"{safe_summary}\n\n"
            f"📊 원문 길이: {len(context.text)}자"
        )

    async def _handle_weather(self, context: RequestContext) -> str:
        if not context.text:
            return (
                "🌤️ **날씨 사용법**\n\n"
                f"`@TeleMonBot 날씨 [도시명]`\n\n"
                f"예시: `@TeleMonBot 날씨 서울`\n"
                f"예시: `@TeleMonBot weather London`"
            )

        prompt = (
            f"Provide current weather information for {context.text}.\n"
            f"Include: current temperature, weather conditions (sunny/cloudy/rainy), "
            f"humidity, and wind speed.\n"
            f"If you don't have real-time data, provide general climate information "
            f"for that location.\n"
            f"Format the response nicely with emojis. Respond in Korean."
        )
        system_prompt = (
            "You are a weather assistant. Provide accurate, concise weather info. "
            "Use emojis to make it engaging. Always respond in Korean."
        )

        weather_info = await _call_ai(prompt, context, system_prompt=system_prompt)
        safe_weather = weather_info.replace("`", "'")

        return f"🌤️ **{context.text} 날씨 정보**\n\n{safe_weather}"

    async def _handle_news(self, context: RequestContext) -> str:
        topic = context.text or "종합"

        prompt = (
            f"Provide a brief news summary about '{topic}'.\n"
            f"Include 3-5 key headlines with 1-line descriptions each.\n"
            f"If you don't have very recent news, provide the most notable "
            f"developments you know about.\n"
            f"Format with emojis and bullet points. Respond in Korean."
        )
        system_prompt = (
            "You are a news assistant. Provide concise, factual news summaries. "
            "Use emojis and bullet points. Always respond in Korean."
        )

        news_info = await _call_ai(prompt, context, system_prompt=system_prompt)
        safe_news = news_info.replace("`", "'")

        return f"📰 **{topic} 뉴스 요약**\n\n{safe_news}"

    async def _handle_register_command(self, context: RequestContext) -> str:
        """Register a new custom command at runtime.

        Usage: 등록 [명령어이름] [프롬프트]
        Example: @TeleMonBot 등록 맞춤법 한국어 맞춤법을 검사하고 수정해줘
        """
        if not context.text:
            return (
                "📝 **명령어 등록 사용법**\n\n"
                f"`@TeleMonBot 등록 [명령어이름] [프롬프트]`\n\n"
                f"예시: `@TeleMonBot 등록 맞춤법 한국어 맞춤법을 검사하고 수정해줘`"
            )

        parts = context.text.split(maxsplit=1)
        cmd_name = parts[0].strip()
        cmd_prompt = parts[1].strip() if len(parts) > 1 else ""

        if not cmd_name:
            return "명령어 이름을 입력해주세요."
        if not cmd_prompt:
            return f"'{cmd_name}' 명령어의 프롬프트를 입력해주세요."

        system_prompt = cmd_prompt

        # 메모리 등록
        async def custom_handler(ctx: RequestContext) -> str:
            prompt = ctx.text or "실행해줘"
            result = await _call_ai(prompt, ctx, system_prompt=system_prompt)
            return result.replace("`", "'")

        self.add_command(cmd_name, custom_handler)

        # 메모리에는 항상 등록 (DB 실패와 무관)
        self._custom_command_names.add(cmd_name)

        # DB 저장 (서버 재시저 후에도 유지, 실패해도 메모리 등록은 유지)
        try:
            bot_db.save_custom_command(cmd_name, system_prompt)
        except Exception:
            logger.warning("[guest] failed to save custom command '%s' to DB (in-memory only)", cmd_name)

        return (
            f"✅ **'{cmd_name}' 명령어가 등록되었습니다!**\n\n"
            f"이제 `@TeleMonBot {cmd_name} [내용]` 으로 사용할 수 있습니다."
        )

    async def _handle_help(self, context: RequestContext) -> str:
        return GUEST_HELP_TEXT

    async def _handle_start(self, context: RequestContext) -> str:
        return (
            "👋 **TeleMon AI 비서입니다!**\n\n"
            "저를 @멘션하면 언제든지:\n"
            "• 🌐 **번역** — 실시간 언어 번역\n"
            "• 📋 **요약** — 긴 글 핵심 요약\n"
            "• 🌤️ **날씨** — 전세계 날씨 정보\n"
            "• 📰 **뉴스** — 최신 뉴스 요약\n\n"
            "을 도와드립니다.\n\n"
            "💡 **TeleMon의 모든 기능**을 사용하려면\n"
            f"👉 telemon.online 에서 가입하세요!"
        )

    async def _handle_fallback(self, context: RequestContext) -> str:
        """등록되지 않은 명령어에 대한 fallback."""
        bad = context.command or (context.text.split()[0] if context.text else "알 수 없는")

        # 기본 명령어 목록 (중복 제거, 한글 우선)
        builtin_lines = [
            "• `@TeleMonBot 번역 [텍스트]`",
            "• `@TeleMonBot 요약 [텍스트]`",
            "• `@TeleMonBot 날씨 [도시]`",
            "• `@TeleMonBot 뉴스 [주제]`",
            "• `@TeleMonBot 등록 [이름] [프롬프트]`",
            "• `@TeleMonBot 도움말`",
        ]

        # Python 3.11 호환: f-string 내에서 backslash 사용 불가 → 변수로 분리
        builtin_section = "\n".join(builtin_lines)

        # 등록된 커스텀 명령어가 있으면 추가
        custom_lines = []
        if self._custom_command_names:
            for name in sorted(self._custom_command_names):
                custom_lines.append(f"• `@TeleMonBot {name} [내용]`")

        custom_section = (
            "\n📌 **커스텀 명령어:**\n" + "\n".join(custom_lines) + "\n"
            if custom_lines else ""
        )

        return (
            f"🤔 죄송합니다. '{bad}' 명령어를 이해하지 못했어요.\n\n"
            f"**사용 가능한 명령어:**\n"
            f"{builtin_section}\n"
            f"{custom_section}"
            f"\n💡 `@TeleMonBot 등록 [이름] [프롬프트]` 로 새 명령어를 등록할 수 있어요!"
        )

    # ── Custom commands DB persistence ───────────────────────────────

    def _load_custom_commands_from_db(self) -> None:
        """DB에서 저장된 커스텀 명령어를 _commands에 등록.

        모든 DB 커스텀 명령어는 동일한 _handle_custom_db_command 핸들러를
        사용하며, 핸들러 내부에서 ctx.command로 system_prompt를 조회합니다.
        """
        try:
            commands = bot_db.load_custom_commands()
            for cmd in commands:
                name = cmd["name"]
                self._custom_db_prompts[name] = cmd["system_prompt"]
                self._custom_command_names.add(name)
                self._commands[name] = self._handle_custom_db_command

            if commands:
                logger.info("[guest] loaded %d custom commands from DB", len(commands))
        except Exception:
            logger.debug("[guest] no custom commands table yet (first run)")

    async def _handle_custom_db_command(self, context: RequestContext) -> str:
        """DB에서 로드된 커스텀 명령어를 실행하는 공유 핸들러.

        context.command로 시스템 프롬프트를 조회하여 _call_ai()를 호출합니다.
        모든 DB 커스텀 명령어가 이 핸들러를 공유합니다.
        """
        name = context.command
        system_prompt = self._custom_db_prompts.get(name, "")
        if not system_prompt:
            return f"⚠️ '{name}' 명령어의 설정을 찾을 수 없습니다."
        prompt = context.text or "실행해줘"
        result = await _call_ai(prompt, context, system_prompt=system_prompt)
        return result.replace("`", "'")

    # ── Daily usage rotation ───────────────────────────────────────

    def _rotate_date(self) -> None:
        """날짜가 변경되었으면 daily_usage 를 리셋."""
        today = datetime.now(timezone.utc).date()
        if today != self._today:
            self._daily_usage.clear()
            self._today = today
