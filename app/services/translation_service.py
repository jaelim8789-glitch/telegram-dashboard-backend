"""Lightweight translation service backed by DeepSeek.

Translates text between languages using the same DeepSeek infrastructure
already powering all TeleMon AI features. No external translation API needed.
"""

from __future__ import annotations

import httpx
from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_TRANSLATION_MODEL = "deepseek-chat"


async def translate_text(
    text: str,
    target_lang: str = "ko",
    source_lang: str | None = None,
) -> str | None:
    """Translate *text* into *target_lang* using DeepSeek.

    Returns translated text or None on failure.
    """
    if not settings.deepseek_api_key:
        logger.warning("translate_skipped_no_api_key")
        return None

    if not text.strip():
        return text

    lang_name = {
        "ko": "Korean",
        "en": "English",
        "ja": "Japanese",
        "zh": "Chinese",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "ru": "Russian",
        "pt": "Portuguese",
        "ar": "Arabic",
    }.get(target_lang, target_lang)

    if source_lang:
        src_name = lang_name if source_lang in lang_name else source_lang
        prompt = (
            f"Translate the following text from {src_name} to {lang_name}. "
            f"Output ONLY the translated text, nothing else.\n\n{text}"
        )
    else:
        prompt = (
            f"Translate the following text to {lang_name}. "
            f"Output ONLY the translated text, nothing else.\n\n{text}"
        )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.deepseek_api_base}/chat/completions",
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                json={
                    "model": _TRANSLATION_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1024,
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.error("translate_failed", error=str(exc))
        return None
