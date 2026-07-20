"""Manual verification: style_profile_id -> get_style_prompt_for_generation."""

import pytest

from app.crud.style_profile import get_style_profile
from app.services.ai_style_service import get_style_prompt_for_generation
from app.models.style_profile import StyleProfile


@pytest.mark.asyncio
async def test_style_profile_prompt_roundtrip(db_session):
    profile = StyleProfile(
        name="Test Style",
        source_type="sample",
        source_text="sample text",
        tone_analysis={},
        style_prompt="You are a playful, emoji-heavy marketer. Use lots of emojis and exclamation marks.",
    )
    db_session.add(profile)
    await db_session.commit()
    await db_session.refresh(profile)

    result = await get_style_prompt_for_generation(profile.id, db_session)
    assert result == profile.style_prompt

    missing = await get_style_prompt_for_generation("non-existent-id", db_session)
    assert missing == ""
