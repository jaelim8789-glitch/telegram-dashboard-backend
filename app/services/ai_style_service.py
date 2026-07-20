import json

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.style_profile import StyleProfile
from app.services.ai_core_service import call_deepseek

logger = get_logger(__name__)

_STYLE_ANALYSIS_PROMPT = (
    "You are a writing style analyst. Analyze the following Korean text and extract its writing style characteristics.\n\n"
    "Return ONLY a JSON object (no markdown, no code fences) with these fields:\n"
    "{\n"
    '  "tone": "formal|casual|friendly|professional|humorous|persuasive|neutral",\n'
    '  "formality_level": 0.0-1.0,\n'
    '  "avg_sentence_length": "short|medium|long",\n'
    '  "emoji_usage": "none|rare|moderate|heavy",\n'
    '  "emoji_types": ["list of emoji categories used, if any"],\n'
    '  "vocabulary_style": "simple|moderate|sophisticated|technical",\n'
    '  "key_patterns": ["distinctive phrasing patterns observed"],\n'
    '  "punctuation_style": "description of punctuation usage",\n'
    '  "greeting_style": "typical opening pattern",\n'
    '  "closing_style": "typical closing pattern",\n'
    '  "summary": "2-3 sentence summary of the overall writing style"\n'
    "}\n\n"
    "Text to analyze:\n\n{text}"
)

_STYLE_PROMPT_TEMPLATE = (
    "Write in the following style:\n"
    "- Tone: {tone}\n"
    "- Formality: {formality_label}\n"
    "- Sentence length: {avg_sentence_length}\n"
    "- Emoji usage: {emoji_usage}\n"
    "- Vocabulary: {vocabulary_style}\n"
    "- Key patterns: {key_patterns}\n"
    "- Punctuation: {punctuation_style}\n"
    "- Greeting style: {greeting_style}\n"
    "- Closing style: {closing_style}\n"
    "- Overall style reference: {summary}"
)


async def analyze_style(name: str, source_type: str, source_text: str, db: AsyncSession) -> StyleProfile:
    reply, _ = await call_deepseek(
        messages=[
            {"role": "system", "content": "You are a precise writing style analyst. Always respond with valid JSON only."},
            {"role": "user", "content": _STYLE_ANALYSIS_PROMPT.format(text=source_text)},
        ],
        max_tokens=2000,
    )

    if not reply:
        raise ValueError("AI 분석 응답을 받지 못했습니다. DeepSeek API 키를 확인해주세요.")

    try:
        analysis = json.loads(reply)
    except json.JSONDecodeError:
        try:
            start = reply.index("{")
            end = reply.rindex("}") + 1
            analysis = json.loads(reply[start:end])
        except (ValueError, json.JSONDecodeError):
            logger.error("style_analysis_parse_failed", raw=reply[:500])
            raise ValueError("AI 응답을 분석할 수 없습니다. 다시 시도해주세요.")

    tone = analysis.get("tone", "neutral")
    formality = analysis.get("formality_level", 0.5)
    formality_label = "높음" if formality >= 0.7 else "낮음" if formality <= 0.3 else "중간"
    emoji = analysis.get("emoji_usage", "none")
    vocab = analysis.get("vocabulary_style", "moderate")
    patterns = analysis.get("key_patterns", [])
    punct = analysis.get("punctuation_style", "")
    greeting = analysis.get("greeting_style", "")
    closing = analysis.get("closing_style", "")
    summary = analysis.get("summary", "")

    style_prompt = _STYLE_PROMPT_TEMPLATE.format(
        tone=tone,
        formality_label=formality_label,
        avg_sentence_length=analysis.get("avg_sentence_length", "medium"),
        emoji_usage=emoji,
        vocabulary_style=vocab,
        key_patterns=", ".join(patterns) if patterns else "없음",
        punctuation_style=punct or "표준",
        greeting_style=greeting or "없음",
        closing_style=closing or "없음",
        summary=summary or f"{tone} 톤의 {formality_label} 형식감을 가진 스타일",
    )

    profile = StyleProfile(
        name=name,
        source_type=source_type,
        source_text=source_text[:50000],
        tone_analysis=analysis,
        style_prompt=style_prompt,
    )
    db.add(profile)
    await db.flush()
    await db.refresh(profile)

    logger.info("style_profile_created", profile_id=profile.id, name=name)
    return profile


async def list_profiles(db: AsyncSession) -> list[StyleProfile]:
    result = await db.execute(select(StyleProfile).order_by(desc(StyleProfile.created_at)))
    return list(result.scalars().all())


async def get_profile(db: AsyncSession, profile_id: str) -> StyleProfile | None:
    return await db.get(StyleProfile, profile_id)


async def update_profile(db: AsyncSession, profile: StyleProfile, name: str) -> StyleProfile:
    profile.name = name
    await db.flush()
    await db.refresh(profile)
    return profile


async def delete_profile(db: AsyncSession, profile: StyleProfile) -> None:
    await db.delete(profile)
    await db.flush()
