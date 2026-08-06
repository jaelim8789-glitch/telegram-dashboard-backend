"""
Ollama API 기반 챗봇 서비스 — OpenAI 호환 API 그대로 사용

ai_core_service.call_ollama()를 단일 클라이언트로 사용합니다.
"""
import json
import logging
import re
from typing import Optional

from app.services.ai_core_service import call_ollama

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """당신은 TeleMon 미니앱의 AI 어시스턴트입니다.
사용자의 질문에 간결하고 도움이 되는 답변을 한국어로 제공하세요.

사용자가 "발송해줘", "보내줘" 등 발송 의도를 표현하면 
반드시 응답 마지막에 다음 JSON 태그를 포함하세요:
<ACTION>{"type":"redirect_send","message":"발송할 메시지"}</ACTION>

사용자가 계정 상태/통계를 물으면:
<ACTION>{"type":"show_stats"}</ACTION>

사용 가능한 정보: 발송 현황, 계정 건강, 토큰 잔액, 최근 발송 내역
답변은 200자 이내로 간결하게."""


async def chat_with_ollama(messages: list[dict], api_key: str = "") -> str:
    """Ollama API를 호출합니다. ai_core_service의 call_ollama를 사용합니다."""
    full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    reply, _tokens, _tool_calls = await call_ollama(full_messages, max_tokens=500)
    if reply is None:
        return "죄송합니다. 일시적인 오류가 발생했습니다."
    return reply


def parse_action(text: str) -> Optional[dict]:
    m = re.search(r'<ACTION>(.*)</ACTION>', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None
    return None
