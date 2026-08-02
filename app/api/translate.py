"""Translate API — lightweight endpoint for in-chat auto-translation.

POST /api/translate
  body: { "text": "...", "target_lang": "ko", "source_lang": "en" | null }
  returns: { "translated_text": "...", "source_lang": "auto-detected" }
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.translation_service import translate_text

router = APIRouter(tags=["translate"])


class TranslateRequest(BaseModel):
    text: str = Field(..., max_length=5000, description="Text to translate")
    target_lang: str = Field(default="ko", max_length=10, description="Target language code")
    source_lang: str | None = Field(default=None, max_length=10, description="Source language code (auto-detect if null)")


class TranslateResponse(BaseModel):
    translated_text: str
    source_lang: str | None = None
    target_lang: str


@router.post("/api/translate", response_model=TranslateResponse)
async def translate_endpoint(payload: TranslateRequest):
    """Translate text using DeepSeek AI.

    Lightweight — no auth required for basic usage.
    Rate limiting handled at the infrastructure level.
    """
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
