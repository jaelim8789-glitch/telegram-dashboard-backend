"""Lightweight translation service backed by DeepSeek.

Translates text between languages using the same DeepSeek infrastructure
already powering all TeleMon AI features. No external translation API needed.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.services.ai_core_service import call_deepseek

logger = get_logger(__name__)


async def translate_text(
    text: str,
    target_lang: str = "ko",
    source_lang: str | None = None,
) -> str | None:
    """Translate *text* into *target_lang* using DeepSeek.

    Returns translated text or None on failure.
    """
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

    reply, _tokens, _tool_calls = await call_deepseek(
        [{"role": "user", "content": prompt}],
        max_tokens=1024,
    )
    if reply is None:
        logger.error("translate_failed")
        return None
    return reply.strip()
