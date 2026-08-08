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
import time # Import time for performance measurement
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
_OLLAMA_KEEP_ALIVE = "10m" # Keep the model loaded for 10 minutes after a request

# ─── Global HTTPX Client for Connection Pooling ───────────────────────────
# Creating a single client instance to be reused for connection pooling and keep-alive benefits.
_ollama_client = httpx.AsyncClient(timeout=60, limits=httpx.Limits(max_keepalive_connections=5, max_connections=10))

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
        "keep_alive": _OLLAMA_KEEP_ALIVE, # Add keep_alive parameter
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    start_time = time.time()
    first_token_time = None
    token_count = 0

    try:
        # Use the global client instance for connection pooling
        response = await _ollama_client.post(
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

        # Performance calculation
        total_time = time.time() - start_time
        # For non-streaming, we consider the whole response time as TTFT
        ttft = total_time
        tokens_per_sec = tokens / total_time if total_time > 0 else 0

        logger.info(f"Ollama API call completed. TTFT: {ttft:.2f}s, Total Time: {total_time:.2f}s, Tokens: {tokens}, Tokens/sec: {tokens_per_sec:.2f}")

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
                # Retry with global client
                retry = await _ollama_client.post(
                    f"{settings.ollama_api_base}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.ollama_api_key}"},
                    json=payload,
                )
                retry.raise_for_status()
                data = retry.json()
                msg = data["choices"][0]["message"]
                usage = data.get("usage", {})
                tokens = usage.get("total_tokens", 0)
                total_time_retry = time.time() - start_time
                logger.info(f"Ollama API retry completed. Total Time: {total_time_retry:.2f}s, Tokens: {tokens}")

                return msg.get("content", "") or "", tokens, msg.get("tool_calls")
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
    start_time = time.time()
    try:
        # Use the global client instance for connection pooling
        response = await _ollama_client.post(
            f"{settings.ollama_api_base}/api/chat",
            json={
                "model": model or settings.ollama_model,
                "messages": messages,
                "stream": False,
                "keep_alive": _OLLAMA_KEEP_ALIVE, # Add keep_alive parameter for local call too
            },
        )
        response.raise_for_status()
        data = response.json()
        content = data.get("message", {}).get("content", "") or ""
        tokens = data.get("eval_count", 0) + data.get("prompt_eval_count", 0)

        total_time = time.time() - start_time
        tokens_per_sec = tokens / total_time if total_time > 0 else 0
        logger.info(f"Ollama Local API call completed. Total Time: {total_time:.2f}s, Tokens: {tokens}, Tokens/sec: {tokens_per_sec:.2f}")

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
    start_time = time.time()
    first_token_time = None
    token_count = 0

    try:
        # Use the global client instance for connection pooling
        async with _ollama_client.stream(
            "POST",
            f"{settings.ollama_api_base}/chat/completions",
            headers={"Authorization": f"Bearer {settings.ollama_api_key}"},
            json={
                "model": model or settings.ollama_model or _DEFAULT_MODEL,
                "messages": messages,
                "max_tokens": max_tokens,
                "stream": True,
                "stream_options": {"include_usage": True},
                "keep_alive": _OLLAMA_KEEP_ALIVE, # Add keep_alive parameter for streaming call too
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
                        if first_token_time is None:
                            first_token_time = time.time()
                            ttft = first_token_time - start_time
                            logger.info(f"Ollama Stream TTFT: {ttft:.2f}s")
                        token_count += 1 # Approximate token count based on content chunks
                        yield (content, 0)
                except (json.JSONDecodeError, IndexError, KeyError):
                    continue

            total_time = time.time() - start_time
            # Calculate tokens/sec based on the time from first token to end, as total generation time
            gen_time = total_time - ttft if ttft else total_time
            tokens_per_sec = token_count / gen_time if gen_time > 0 else 0

            logger.info(f"Ollama Stream completed. TTFT: {ttft:.2f}s, Total Time: {total_time:.2f}s, Approx Tokens: {token_count}, Tokens/sec: {tokens_per_sec:.2f}")
    except Exception as exc:
        logger.error("ai_ollama_stream_error", error=str(exc))
        yield (None, 0)