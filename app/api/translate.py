"""Translate API — lightweight endpoint for in-chat auto-translation.

POST /api/translate
  body: { "text": "...", "target_lang": "ko", "source_lang": "en" | null }
  returns: { "translated_text": "...", "source_lang": "auto-detected" }
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.rate_limiter import check_rate_limit, get_client_ip, get_retry_after_seconds
from app.services.translation_service import translate_text

router = APIRouter(tags=["translate"])

_TRANSLATE_LIMIT = dict(max_attempts=20, window_seconds=60)


class TranslateRequest(BaseModel):
    text: str = Field(..., max_length=5000, description="Text to translate")
    target_lang: str = Field(default="ko", max_length=10, description="Target language code")
    source_lang: str | None = Field(default=None, max_length=10, description="Source language code (auto-detect if null)")


class TranslateResponse(BaseModel):
    translated_text: str
    source_lang: str | None = None
    target_lang: str


@router.post("/api/translate", response_model=TranslateResponse)
async def translate_endpoint(payload: TranslateRequest, request: Request):
    """Translate text using DeepSeek AI.

    Lightweight — no auth required for basic usage (translations happen during
    chat viewing). Rate-limited per IP to prevent AI cost abuse.
    """
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "translate", **_TRANSLATE_LIMIT):
        retry_after = get_retry_after_seconds(client_ip, "translate", _TRANSLATE_LIMIT["window_seconds"])
        raise HTTPException(
            status_code=429,
            detail="Too many translation requests. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    result = await translate_text(
        text=payload.text,
        target_lang=payload.target_lang,
        source_lang=payload.source_lang,
    )

    if result is None:
        raise HTTPException(status_code=502, detail="번역 서비스에 연결할 수 없습니다.")

    return TranslateResponse(
        translated_text=result,
        source_lang=payload.source_lang,
        target_lang=payload.target_lang,
    )
