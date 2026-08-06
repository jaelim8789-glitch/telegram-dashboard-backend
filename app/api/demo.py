"""Public, unauthenticated demo AI chat endpoint.

Powers the marketing /demo/chat page. Stateless — no session/DB
persistence, no tenant context. Rate-limited per IP since it is
reachable without login and spends a real Ollama API budget.
"""

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.core.rate_limiter import check_rate_limit, get_client_ip
from app.services.ai_core_service import call_ollama

router = APIRouter(prefix="/api/demo", tags=["demo"])
logger = get_logger(__name__)

_MAX_MESSAGE_LENGTH = 500
_MAX_HISTORY_TURNS = 10
_RATE_LIMIT_MAX = 8
_RATE_LIMIT_WINDOW_SECONDS = 60

_DEMO_SYSTEM_PROMPT = """당신은 TeleMon의 AI 어시스턴트 데모입니다. TeleMon은 텔레그램 계정 운영을 자동화하는 플랫폼으로,
다음 기능을 제공합니다:
- 대량 발송(브로드캐스트)과 반복 예약 발송
- 계정 상태(인증 만료, 차단, 속도 제한) 실시간 모니터링
- 키워드 기반 자동 응답 및 답장 매크로
- 그룹/채널 검색 및 관리, 참여 링크 분석

친절하고 간결하게 한국어로 답하고, TeleMon이 이런 상황에서 어떻게 도움이 되는지 자연스럽게 안내하세요.
이것은 영업용 데모이므로 과장 없이 실제 기능 범위 안에서만 안내하고, 계정 정보나 개인정보를 요구하지 마세요."""


class DemoChatTurn(BaseModel):
    role: str
    content: str


class DemoChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=_MAX_MESSAGE_LENGTH)
    history: list[DemoChatTurn] = Field(default_factory=list)


class DemoChatResponse(BaseModel):
    reply: str


@router.post("/ai-chat", response_model=DemoChatResponse)
async def demo_ai_chat(payload: DemoChatRequest, request: Request) -> DemoChatResponse:
    client_ip = get_client_ip(request)
    if not check_rate_limit(
        client_ip, "demo_ai_chat", max_attempts=_RATE_LIMIT_MAX, window_seconds=_RATE_LIMIT_WINDOW_SECONDS
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="요청이 너무 많습니다. 잠시 후 다시 시도해주세요.",
        )

    messages: list[dict] = [{"role": "system", "content": _DEMO_SYSTEM_PROMPT}]
    for turn in payload.history[-_MAX_HISTORY_TURNS:]:
        if turn.role in ("user", "assistant"):
            messages.append({"role": turn.role, "content": turn.content[:_MAX_MESSAGE_LENGTH]})
    messages.append({"role": "user", "content": payload.message})

    reply, _tokens, _tool_calls = await call_ollama(messages, max_tokens=500)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI 응답을 가져오지 못했습니다. 잠시 후 다시 시도해주세요.",
        )

    return DemoChatResponse(reply=reply)
