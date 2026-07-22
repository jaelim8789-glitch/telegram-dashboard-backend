"""
DeepSeek API 기반 챗봇 서비스 — OpenAI 호환 API 그대로 사용
"""
import os
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

SYSTEM_PROMPT = """당신은 TeleMon 미니앱의 AI 어시스턴트입니다.
사용자의 질문에 간결하고 도움이 되는 답변을 한국어로 제공하세요.

사용자가 "발송해줘", "보내줘" 등 발송 의도를 표현하면 
반드시 응답 마지막에 다음 JSON 태그를 포함하세요:
<ACTION>{"type":"redirect_send","message":"발송할 메시지"}</ACTION>

사용자가 계정 상태/통계를 물으면:
<ACTION>{"type":"show_stats"}</ACTION>

사용 가능한 정보: 발송 현황, 계정 건강, 토큰 잔액, 최근 발송 내역
답변은 200자 이내로 간결하게."""

import httpx

async def chat_with_deepseek(messages: list[dict], api_key: str = "") -> str:
    key = api_key or DEEPSEEK_API_KEY
    if not key:
        return "죄송합니다. AI 서비스가 설정되지 않았습니다."

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                DEEPSEEK_API_URL,
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                    "temperature": 0.7,
                    "max_tokens": 500,
                },
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as e:
        logger.error(f"DeepSeek API HTTP error: {e.response.status_code} {e.response.text}")
        return f"AI 서비스 오류 (HTTP {e.response.status_code})"
    except Exception as e:
        logger.error(f"DeepSeek API error: {e}")
        return "죄송합니다. 일시적인 오류가 발생했습니다."

def parse_action(text: str) -> Optional[dict]:
    import re
    m = re.search(r'<ACTION>(.*?)</ACTION>', text, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except: return None
    return None
