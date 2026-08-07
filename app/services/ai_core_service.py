"""
TeleMon AI Core Service — shared Ollama client, Graphiti memory integration,
and usage tracking for all AI features (Chat, Reply Assistant, Broadcast Assistant,
Operations Report).

Every AI feature routes through this module so that:
- The same Ollama configuration (model, base URL, API key) is used everywhere.
- Graphiti long-term memory is consistently applied per-tenant.
- Usage quotas and credits are enforced uniformly.
- All AI interactions are logged for audit.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────

_MAX_INPUT_CHARS = 4000
_MAX_TOKENS = 1000
_DEFAULT_MODEL = settings.ollama_model or "ollama-chat"

# Feature names for usage tracking
FEATURE_CHAT = "ai_chat"
FEATURE_REPLY_ASSISTANT = "ai_reply_assistant"
FEATURE_BROADCAST_ASSISTANT = "ai_broadcast_assistant"
FEATURE_OPERATIONS_REPORT = "ai_operations_report"
FEATURE_CONTENT_STUDIO = "ai_content_studio"


# ─── Data Classes ─────────────────────────────────────────────────────────


@dataclass
class AiResult:
    """Standard result for all AI feature calls."""
    status: str  # "ok" | "error" | "quota_exceeded" | "rate_limited" | "too_long"
    reply: str | None = None
    detail: str = ""
    tokens_used: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ─── In-flight lock (per-tenant per-feature) ──────────────────────────────

_in_flight: dict[str, bool] = {}  # key = f"{tenant_id}:{feature}"


def _is_in_flight(tenant_id: str, feature: str) -> bool:
    return _in_flight.get(f"{tenant_id}:{feature}", False)


def _set_in_flight(tenant_id: str, feature: str, value: bool) -> None:
    key = f"{tenant_id}:{feature}"
    if value:
        _in_flight[key] = True
    else:
        _in_flight.pop(key, None)


# ─── Ollama API Call ─────────────────────────────────────────────────────


async def call_ollama(
    messages: list[dict],
    max_tokens: int = _MAX_TOKENS,
    model: str | None = None,
    tools: list[dict] | None = None,
    json_mode: bool = False,
) -> tuple[str | None, int, list[dict] | None]:
    """Call Ollama API and return (reply_text, tokens_used, tool_calls).

    Returns (None, 0, None) on any failure.
    If tools are provided, the response may include tool_calls instead of content.
    If json_mode is True, requests a strict JSON object response via
    response_format (OpenAI-compatible endpoint).
    """
    payload: dict = {
        "model": model or settings.ollama_model or _DEFAULT_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{settings.ollama_api_base}/chat/completions",
                headers={"Authorization": f"Bearer {settings.ollama_api_key}"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]

            content = message.get("content", "") or ""
            tool_calls = message.get("tool_calls")
            usage = data.get("usage", {})
            tokens = usage.get("total_tokens", 0)

            return content, tokens, tool_calls
    except httpx.TimeoutException:
        logger.error("ai_ollama_timeout")
        return None, 0, None
    except httpx.HTTPStatusError as exc:
        # Some self-hosted servers reject response_format (json_mode) with a
        # 4xx. Degrade gracefully: retry once without it so callers that only
        # needed a JSON-shaped reply still get one (parse layer handles it).
        if json_mode and exc.response.status_code in (400, 404, 422):
            logger.warning(
                "ai_ollama_json_mode_unsupported_retry_plain",
                status=exc.response.status_code,
            )
            try:
                payload.pop("response_format", None)
                async with httpx.AsyncClient(timeout=60) as retry_client:
                    retry = await retry_client.post(
                        f"{settings.ollama_api_base}/chat/completions",
                        headers={"Authorization": f"Bearer {settings.ollama_api_key}"},
                        json=payload,
                    )
                    retry.raise_for_status()
                    data = retry.json()
                    msg = data["choices"][0]["message"]
                    usage = data.get("usage", {})
                    return msg.get("content", "") or "", usage.get("total_tokens", 0), msg.get("tool_calls")
            except (httpx.HTTPError, KeyError, IndexError, ValueError, json.JSONDecodeError) as rerr:
                logger.error("ai_ollama_retry_plain_failed", error=str(rerr))
                return None, 0, None
        logger.error("ai_ollama_http_error", status=exc.response.status_code, body=exc.response.text[:500])
        return None, 0, None
    except (httpx.HTTPError, KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        logger.error("ai_ollama_call_failed", error=str(exc))
        return None, 0, None


# ─── Local LLM (Ollama) call — free-plan tenants ──────────────────────────


async def call_ollama_local(
    messages: list[dict],
    model: str | None = None,
) -> tuple[str | None, int, list[dict] | None]:
    """Call the local Ollama server and return (reply_text, tokens_used, tool_calls).

    Same return shape as call_ollama so callers can treat them
    interchangeably. tool_calls is always None -- the local model isn't
    wired up for tool-calling. Returns (None, 0, None) on any failure so a
    down/unreachable Ollama degrades the same way a missing Ollama key
    does, instead of crashing the request.
    """
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{settings.ollama_api_base}/api/chat",
                json={
                    "model": model or settings.ollama_model,
                    "messages": messages,
                    "stream": False,
                    "keep_alive": 1800,
                },
            )
            response.raise_for_status()
            data = response.json()
            content = data.get("message", {}).get("content", "") or ""
            tokens = data.get("eval_count", 0) + data.get("prompt_eval_count", 0)
            return content, tokens, None
    except httpx.TimeoutException:
        logger.error("ai_ollama_timeout")
        return None, 0, None
    except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
        logger.error("ai_ollama_call_failed", error=str(exc))
        return None, 0, None


async def call_llm_for_tenant(
    tenant_plan: str,
    messages: list[dict],
    max_tokens: int = _MAX_TOKENS,
    model: str | None = None,
    tools: list[dict] | None = None,
) -> tuple[str | None, int, list[dict] | None]:
    """Routes to the local Ollama model for free-plan tenants, Ollama for
    everyone else (pro/team) -- keeps Ollama's per-message API cost off the
    much larger free user base. Falls back to Ollama if Ollama fails, so a
    free user still gets a real answer instead of a dead end (at the cost of
    the Ollama call that routing was meant to avoid, but only on failure)."""
    if settings.ollama_enabled and tenant_plan == "free":
        reply, tokens, _ = await call_ollama_local(messages, model=None)
        if reply is not None:
            return reply, tokens, None
        logger.warning("ollama_failed_falling_back_to_ollama")
    return await call_ollama(messages, max_tokens=max_tokens, model=model, tools=tools)


async def _call_ollama_stream(
    messages: list[dict],
    max_tokens: int = _MAX_TOKENS,
    model: str | None = None,
):
    """SSE 스트리밍 버전: async generator가 (content, usage_total_tokens) tuple을 yield.

    마지막 청크는 content="" 이고 usage_total_tokens에 실제 토큰 수를 담아
    전달된다 (stream_options.include_usage=True). content-only 호출부와 호환되도록
    content 문자열과 함께 정수를 yield한다.
    """
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                f"{settings.ollama_api_base}/chat/completions",
                headers={"Authorization": f"Bearer {settings.ollama_api_key}"},
                json={
                    "model": model or settings.ollama_model or _DEFAULT_MODEL,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                        # Usage chunk: choices is empty, usage present at stream end.
                        if not chunk.get("choices") and "usage" in chunk:
                            total = chunk["usage"].get("total_tokens", 0) or 0
                            yield ("", total)
                            continue
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield (content, 0)
                    except (json.JSONDecodeError, IndexError, KeyError):
                        continue
    except Exception as exc:
        logger.error("ai_ollama_stream_error", error=str(exc))
        yield (None, 0)


# ─── Graphiti Memory Integration ──────────────────────────────────────────


_memory_provider = None


def _get_memory_provider():
    global _memory_provider
    if _memory_provider is None:
        from app.services.ai_memory import get_ai_memory_provider

        _memory_provider = get_ai_memory_provider()
    return _memory_provider


async def store_memory(
    tenant_id: str,
    name: str,
    episode_body: str,
    source: str = "text",
    source_description: str = "",
) -> None:
    """Store an episode in Graphiti long-term memory.

    This is a no-op if Graphiti is not configured.
    """
    provider = _get_memory_provider()
    if provider is None:
        return
    try:
        await provider.add_episode(
            tenant_id,
            episode_body,
            {"name": name, "source": source, "source_description": source_description},
        )
    except Exception as exc:
        logger.warning("graphiti_store_failed", error=str(exc))


async def search_memory(
    tenant_id: str,
    query: str,
    max_results: int = 5,
) -> list[dict]:
    """Search Graphiti long-term memory for relevant context.

    Returns empty list if Graphiti is not configured or search fails.
    """
    provider = _get_memory_provider()
    if provider is None:
        return []
    try:
        return await provider.search(tenant_id, query, limit=max_results)
    except Exception as exc:
        logger.warning("graphiti_search_failed", error=str(exc))
        return []


# ─── Usage Tracking ───────────────────────────────────────────────────────


async def check_ai_quota(
    db: AsyncSession,
    tenant_id: str,
    feature: str,
) -> tuple[bool, str]:
    """Check if tenant has quota remaining for the given AI feature.

    Returns (allowed, reason). If allowed is False, reason explains why.
    """
    # Get plan limits for this feature
    result = await db.execute(
        select(AiPlanLimit).where(
            AiPlanLimit.plan == await _get_tenant_plan(db, tenant_id),
            AiPlanLimit.feature == feature,
        )
    )
    limit = result.scalar_one_or_none()

    if limit is None:
        # No specific limit configured — allow by default
        return True, ""

    if not limit.is_enabled:
        return False, "이 AI 기능이 현재 요금제에서 비활성화되었습니다."

    # Check daily request count
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_result = await db.execute(
        select(func.count(AiUsageRecord.id)).where(
            AiUsageRecord.tenant_id == tenant_id,
            AiUsageRecord.feature == feature,
            AiUsageRecord.created_at >= today_start,
        )
    )
    today_count = today_result.scalar() or 0

    if limit.max_requests_per_day > 0 and today_count >= limit.max_requests_per_day:
        return False, f"일일 AI 사용 한도({limit.max_requests_per_day}회)를 초과했습니다. 내일 다시 시도해주세요."

    # Check daily token count
    today_tokens_result = await db.execute(
        select(func.coalesce(func.sum(AiUsageRecord.tokens_used), 0)).where(
            AiUsageRecord.tenant_id == tenant_id,
            AiUsageRecord.feature == feature,
            AiUsageRecord.created_at >= today_start,
        )
    )
    today_tokens = today_tokens_result.scalar() or 0

    if limit.max_tokens_per_day > 0 and today_tokens >= limit.max_tokens_per_day:
        return False, f"일일 토큰 사용 한도를 초과했습니다."

    return True, ""


async def record_ai_usage(
    db: AsyncSession,
    tenant_id: str,
    feature: str,
    tokens_used: int = 0,
    requests_count: int = 1,
    cost_credits: float = 0.0,
) -> None:
    """Record AI usage for a tenant."""
    record = AiUsageRecord(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        feature=feature,
        tokens_used=tokens_used,
        requests_count=requests_count,
        cost_credits=cost_credits,
    )
    db.add(record)


async def get_ai_usage_summary(
    db: AsyncSession,
    tenant_id: str,
    days: int = 30,
) -> dict[str, Any]:
    """Get AI usage summary for a tenant."""
    since = datetime.now(timezone.utc).replace(tzinfo=None) - __import__("datetime").timedelta(days=days)

    result = await db.execute(
        select(
            AiUsageRecord.feature,
            func.count(AiUsageRecord.id).label("requests"),
            func.coalesce(func.sum(AiUsageRecord.tokens_used), 0).label("tokens"),
            func.coalesce(func.sum(AiUsageRecord.cost_credits), 0.0).label("credits"),
        ).where(
            AiUsageRecord.tenant_id == tenant_id,
            AiUsageRecord.created_at >= since,
        ).group_by(AiUsageRecord.feature)
    )
    rows = result.all()

    summary = {}
    total_tokens = 0
    total_credits = 0.0
    for row in rows:
        summary[row.feature] = {
            "requests": row.requests,
            "tokens": row.tokens,
            "credits": float(row.credits),
        }
        total_tokens += row.tokens
        total_credits += float(row.credits)

    return {
        "features": summary,
        "total_requests": sum(v["requests"] for v in summary.values()),
        "total_tokens": total_tokens,
        "total_credits": total_credits,
        "period_days": days,
    }


async def _get_tenant_plan(db: AsyncSession, tenant_id: str) -> str:
    """Get the plan name for a tenant from the Tenant table.

    Previously hard-coded to "free", which silently capped paid (pro/team)
    tenants at the free AI quota. Now resolves the real plan so paid limits
    apply. Falls back to "free" only when the tenant row is missing.
    """
    from app.models.tenant import Tenant

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not getattr(tenant, "plan", ""):
        return "free"
    return tenant.plan


# ─── System Prompt Templates ──────────────────────────────────────────────

AI_CHAT_SYSTEM_PROMPT = (
    "너는 TeleMon AI 어시스턴트야. TeleMon은 텔레그램 마케팅 자동화 플랫폼이야.\n\n"
    "역할:\n"
    "- 사용자의 TeleMon 운영 상태를 이해하고 도움을 제공\n"
    "- 텔레그램 마케팅, 발송 전략, 계정 관리에 대한 전문 지식 제공\n"
    "- 한국어로 친절하고 전문적으로 응답\n\n"
    "정체성:\n"
    "- 너는 TeleMon AI야. TeleMon이 자체 개발하고 지속적으로 파인튜닝하며 발전시키는 독자 AI 모델이야.\n"
    "- 절대 'Gemma', 'Google', '구글', 'LLaMA', 'GPT', 'Claude', '오픈소스 모델' 등 실제 외부 모델/회사 이름을 밝히지 마.\n"
    "- '누구야?', '무슨 모델이야?' 같은 질문에는 \"저는 TeleMon AI입니다\"라고만 답해.\n\n"
    "규칙:\n"
    "- 확신하는 내용만 답하고, 모르거나 확인할 수 없는 것은 절대 지어내지 말 것 (가격·수치·기능·정책 추측 금지)\n"
    "- 요금·가격·견적은 정확히 모르면 지어내지 말고 '정확히 모르겠습니다'라고 답할 것\n"
    "- 불법·유해 요청(해킹 등)은 되묻지 말고 가격/견적을 안내하지 않은 채 바로 정중히 거절하고 답변을 끝낼 것\n"
    "- 액션 가능한 조언을 우선적으로 제공\n"
    "- 필요시 이모지를 적절히 사용\n"
    "- 모르는 것은 솔직히 모른다고 답하고, 질문이 모호하면 짐작으로 답하지 말고 필요한 정보를 구체적으로 되물을 것\n"
    "- \"구체적으로 알려주시면 알아보고 정확히 답해드릴게요\"처럼 안내하고, 사용자가 자세히 질문해줄수록 TeleMon AI가 발전하니 질문을 자연스럽게 유도할 것\n"
    "- 응답은 간결하게"
)

AI_REPLY_ASSISTANT_PROMPT = (
    "너는 TeleMon AI 답장 어시스턴트야. 들어온 메시지에 대한 가장 적절한 답장을 추천해줘.\n\n"
    "규칙:\n"
    "- 자연스럽고 친근한 한국어로 답장 작성\n"
    "- 비즈니스 컨텍스트에 맞게 전문성 유지\n"
    "- 너무 장황하지 않게 (2-3문장 이내)\n"
    "- 필요시 질문에 대한 답변, 정보 제공, 또는 후속 액션 제안 포함\n"
    "- JSON 형식으로 응답: {\"reply\": \"...\", \"confidence\": 0.0~1.0, \"reason\": \"...\"}"
)

AI_BROADCAST_ASSISTANT_PROMPT = (
    "너는 TeleMon AI 브로드캐스트 어시스턴트야. 효과적인 텔레그램 마케팅 메시지를 생성해줘.\n\n"
    "규칙:\n"
    "- 목적과 대상에 맞는 맞춤형 메시지 작성\n"
    "- 한국어로 작성 (다른 언어 요청시 해당 언어로)\n"
    "- 클릭율을 높이는 카피라이팅 기법 활용\n"
    "- 적절한 길이 유지 (100-500자)\n"
    "- JSON 형식으로 응답: {\"message\": \"...\", \"variant_a\": \"...\", \"variant_b\": \"...\", \"reasoning\": \"...\"}"
)

AI_OPERATIONS_REPORT_PROMPT = (
    "너는 TeleMon AI 운영 분석가야. 제공된 운영 데이터를 분석해서 인사이트와 추천을 제공해줘.\n\n"
    "분석 항목:\n"
    "1. 메시지 전달 현황 (발송 수, 성공률, 실패율)\n"
    "2. 계정 상태 분석\n"
    "3. 고객 참여도 분석\n"
    "4. 위험 요소 식별\n"
    "5. 개선 추천 (우선순위 순)\n\n"
    "JSON 형식으로 응답."
)


# ─── Model bindings ────────────────────────────────────────────────────────

# Resolve the real models (previously stubbed as placeholder classes, which
# made check_ai_quota/record_ai_usage query non-existent tables). Kept at
# module load — app.models.ai only depends on app.database.Base, so there is
# no circular-import risk.
from app.models.ai import AiPlanLimit, AiUsageRecord  # noqa: E402