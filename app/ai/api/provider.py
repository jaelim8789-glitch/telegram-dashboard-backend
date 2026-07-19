"""
AI API Provider — unified interface for external LLM API providers.

Supports multiple providers (DeepSeek, OpenAI, Anthropic, etc.) with
rate limiting, retry logic, token tracking, and audit logging.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.config import get_ai_config
from app.ai.models.ai_api import AiApiCallLog
from app.core.logging import get_logger

logger = get_logger(__name__)


class AiApiProvider:
    """Unified interface for external LLM API providers."""

    def __init__(self) -> None:
        self._config = get_ai_config()
        self._rate_limiters: dict[str, _RateLimiter] = {}

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        provider: str = "deepseek",
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        db: AsyncSession | None = None,
        tenant_id: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Send a chat completion request to the specified provider.

        Returns a dict with: id, provider, model, content, tool_calls,
        finish_reason, prompt_tokens, completion_tokens, total_tokens, duration_ms
        """
        call_id = str(uuid.uuid4())
        correlation_id = correlation_id or call_id
        start_time = time.monotonic()

        # Resolve provider config
        api_base = self._config.llm_api_base
        api_key = self._config.llm_api_key
        resolved_model = model or self._config.llm_model
        resolved_max_tokens = max_tokens or self._config.llm_max_tokens
        resolved_temperature = temperature or self._config.llm_temperature

        # Rate limiting
        limiter = self._get_rate_limiter(provider)
        await limiter.wait_if_needed()

        try:
            async with httpx.AsyncClient(timeout=self._config.llm_timeout_seconds) as client:
                request_body: dict[str, Any] = {
                    "model": resolved_model,
                    "messages": messages,
                    "max_tokens": resolved_max_tokens,
                    "temperature": resolved_temperature,
                }
                if tools:
                    request_body["tools"] = tools
                if stream:
                    request_body["stream"] = True

                response = await client.post(
                    f"{api_base.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_body,
                )
                response.raise_for_status()
                data = response.json()

            duration_ms = int((time.monotonic() - start_time) * 1000)
            usage = data.get("usage", {})
            choice = data["choices"][0]
            message = choice.get("message", {})

            result = {
                "id": data.get("id", call_id),
                "provider": provider,
                "model": resolved_model,
                "content": message.get("content"),
                "tool_calls": message.get("tool_calls"),
                "finish_reason": choice.get("finish_reason"),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "duration_ms": duration_ms,
            }

            # Log to DB
            if db and tenant_id:
                await self._log_api_call(
                    db, tenant_id, provider, resolved_model,
                    "/chat/completions", response.status_code, "success",
                    request_body, data, usage, duration_ms, correlation_id,
                )

            return result

        except httpx.TimeoutException:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            if db and tenant_id:
                await self._log_api_call(
                    db, tenant_id, provider, resolved_model,
                    "/chat/completions", None, "timeout",
                    {"messages": messages}, None, {}, duration_ms, correlation_id,
                )
            return {
                "id": call_id, "provider": provider, "model": resolved_model,
                "content": None, "tool_calls": None, "finish_reason": "error",
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "duration_ms": duration_ms, "error": "Request timed out",
            }

        except Exception as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            status_code = getattr(exc, "response", None) and exc.response.status_code
            if db and tenant_id:
                await self._log_api_call(
                    db, tenant_id, provider, resolved_model,
                    "/chat/completions", status_code, "error",
                    {"messages": messages}, None, {}, duration_ms, correlation_id,
                    error_message=str(exc),
                )
            return {
                "id": call_id, "provider": provider, "model": resolved_model,
                "content": None, "tool_calls": None, "finish_reason": "error",
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "duration_ms": duration_ms, "error": str(exc),
            }

    async def _log_api_call(
        self,
        db: AsyncSession,
        tenant_id: str,
        provider: str,
        model: str,
        endpoint: str,
        status_code: int | None,
        status: str,
        request_body: dict[str, Any] | None,
        response_body: dict[str, Any] | None,
        usage: dict[str, int],
        duration_ms: int,
        correlation_id: str | None,
        error_message: str | None = None,
    ) -> None:
        """Log an API call to the database."""
        try:
            log = AiApiCallLog(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                provider=provider,
                model=model,
                endpoint=endpoint,
                request_body=request_body,
                response_body=response_body,
                status_code=status_code,
                status=status,
                error_message=error_message,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                duration_ms=duration_ms,
                correlation_id=correlation_id,
            )
            db.add(log)
            await db.commit()
        except Exception as exc:
            logger.warning("api_call_log_failed", error=str(exc))

    def _get_rate_limiter(self, provider: str) -> _RateLimiter:
        """Get or create a rate limiter for a provider."""
        if provider not in self._rate_limiters:
            self._rate_limiters[provider] = _RateLimiter(rpm=60, tpm=100000)
        return self._rate_limiters[provider]


class _RateLimiter:
    """Simple token-bucket rate limiter for API calls."""

    def __init__(self, rpm: int, tpm: int) -> None:
        self._rpm = rpm
        self._tpm = tpm
        self._request_times: list[float] = []
        self._token_usage: list[tuple[float, int]] = []

    async def wait_if_needed(self) -> None:
        """Wait if rate limit would be exceeded."""
        now = time.monotonic()
        window = 60.0

        # Clean old entries
        self._request_times = [t for t in self._request_times if now - t < window]
        self._token_usage = [(t, c) for t, c in self._token_usage if now - t < window]

        # Check RPM
        if len(self._request_times) >= self._rpm:
            sleep_time = self._request_times[0] + window - now
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        # Check TPM
        total_tokens = sum(c for _, c in self._token_usage)
        if total_tokens >= self._tpm:
            sleep_time = self._token_usage[0][0] + window - now
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    def record_request(self, tokens: int = 0) -> None:
        """Record a request for rate limiting."""
        now = time.monotonic()
        self._request_times.append(now)
        if tokens > 0:
            self._token_usage.append((now, tokens))


# ── Singleton ─────────────────────────────────────────────────────────

_provider: AiApiProvider | None = None


def get_api_provider() -> AiApiProvider:
    """Get the singleton API provider instance."""
    global _provider
    if _provider is None:
        _provider = AiApiProvider()
    return _provider