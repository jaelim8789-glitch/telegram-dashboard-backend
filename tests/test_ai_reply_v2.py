"""Tests for AI Reply 2.0."""

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.ai_reply_v2 import AiReplyPersona, AiReplyConversation, AiReplySuggestionV2
from app.schemas.ai_reply_v2 import (
    PersonaCreate,
    PersonaUpdate,
    SuggestionGenerateRequest,
    SuggestionReviewRequest,
    SuggestionFeedbackRequest,
    PersonaStyle,
    BusinessInfo,
)
from app.services.ai_reply_v2_service import (
    create_persona,
    update_persona,
    list_personas,
    delete_persona,
    get_active_persona,
    get_or_create_conversation,
    add_message_to_conversation,
    generate_suggestions,
    review_suggestion,
    submit_feedback,
    _parse_suggestions,
)


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def sample_persona_create():
    return PersonaCreate(
        name="Professional Assistant",
        tone="professional",
        style=PersonaStyle(
            formality=0.8,
            emoji_usage="minimal",
            greeting_style="formal",
            signature="Best regards",
            max_length=500,
            language="ko",
        ),
        business_info=BusinessInfo(
            company_name="TeleMon Inc.",
            industry="Technology",
            offerings=["Telegram Marketing", "Automation"],
            brand_voice="Professional and reliable",
        ),
    )


@pytest.fixture
def sample_suggestion_request():
    return SuggestionGenerateRequest(
        account_id="test-account-1",
        chat_id="-1001234567890",
        chat_title="Test Group",
        user_id="987654321",
        user_name="Test User",
        incoming_message="안녕하세요, 제품 문의 드립니다.",
        persona_id=None,
        auto_reply_enabled=False,
    )


# ── _parse_suggestions Tests ────────────────────────────────────────────


class TestParseSuggestions:
    def test_valid_json(self):
        raw = json.dumps({
            "primary": {"text": "Hello!", "confidence": 0.95, "reason": "Perfect match"},
            "alternatives": [
                {"text": "Hi there!", "confidence": 0.7, "reason": "Alternative"},
                {"text": "Hey!", "confidence": 0.5, "reason": "Casual option"},
            ],
        })
        result = _parse_suggestions(raw)
        assert result is not None
        assert result["primary"]["text"] == "Hello!"
        assert result["primary"]["confidence"] == 0.95
        assert len(result["alternatives"]) == 2

    def test_markdown_code_block(self):
        raw = "```json\n{\"primary\": {\"text\": \"Hello!\", \"confidence\": 0.9}}\n```"
        result = _parse_suggestions(raw)
        assert result is not None
        assert result["primary"]["text"] == "Hello!"

    def test_invalid_json_returns_none(self):
        result = _parse_suggestions("not json at all")
        assert result is None

    def test_missing_primary_returns_none(self):
        raw = json.dumps({"alternatives": []})
        result = _parse_suggestions(raw)
        assert result is None

    def test_confidence_clamping(self):
        raw = json.dumps({
            "primary": {"text": "Test", "confidence": 1.5, "reason": "Test"},
        })
        result = _parse_suggestions(raw)
        assert result is not None
        assert result["primary"]["confidence"] == 1.0

    def test_empty_alternatives(self):
        raw = json.dumps({
            "primary": {"text": "Test", "confidence": 0.8, "reason": "Test"},
        })
        result = _parse_suggestions(raw)
        assert result is not None
        assert result["alternatives"] == []

    def test_max_three_alternatives(self):
        raw = json.dumps({
            "primary": {"text": "Test", "confidence": 0.8, "reason": "Test"},
            "alternatives": [
                {"text": "A", "confidence": 0.5},
                {"text": "B", "confidence": 0.5},
                {"text": "C", "confidence": 0.5},
                {"text": "D", "confidence": 0.5},
            ],
        })
        result = _parse_suggestions(raw)
        assert result is not None
        assert len(result["alternatives"]) == 3


# ── Persona CRUD Tests ──────────────────────────────────────────────────


class TestPersonaCRUD:
    @pytest.mark.asyncio
    async def test_create_persona(self, db_session, sample_persona_create):
        persona = await create_persona(
            db_session,
            tenant_id="test-tenant",
            account_id="test-account-1",
            payload=sample_persona_create,
        )
        assert persona.id is not None
        assert persona.name == "Professional Assistant"
        assert persona.tone == "professional"
        assert persona.is_active is True  # First persona is always active
        assert persona.style["formality"] == 0.8
        assert persona.business_info["company_name"] == "TeleMon Inc."

    @pytest.mark.asyncio
    async def test_create_multiple_personas(self, db_session, sample_persona_create):
        p1 = await create_persona(db_session, "test-tenant", "test-account-2", sample_persona_create)
        assert p1.is_active is True

        p2_create = PersonaCreate(name="Casual Assistant", tone="casual")
        p2 = await create_persona(db_session, "test-tenant", "test-account-2", p2_create)
        # Second persona should not auto-activate if one is already active
        # (the test creates first, which becomes active; second is not auto-active)
        assert p2.is_active is False

    @pytest.mark.asyncio
    async def test_list_personas(self, db_session, sample_persona_create):
        await create_persona(db_session, "test-tenant", "test-account-3", sample_persona_create)
        p2 = PersonaCreate(name="Casual Assistant", tone="casual")
        await create_persona(db_session, "test-tenant", "test-account-3", p2)

        personas = await list_personas(db_session, "test-account-3")
        assert len(personas) == 2

    @pytest.mark.asyncio
    async def test_update_persona(self, db_session, sample_persona_create):
        persona = await create_persona(db_session, "test-tenant", "test-account-4", sample_persona_create)

        update = PersonaUpdate(name="Updated Name", tone="friendly", is_active=True)
        updated = await update_persona(db_session, persona.id, "test-account-4", update)
        assert updated is not None
        assert updated.name == "Updated Name"
        assert updated.tone == "friendly"
        assert updated.is_active is True

    @pytest.mark.asyncio
    async def test_delete_persona(self, db_session, sample_persona_create):
        persona = await create_persona(db_session, "test-tenant", "test-account-5", sample_persona_create)
        deleted = await delete_persona(db_session, persona.id, "test-account-5")
        assert deleted is True

        # Verify it's gone
        personas = await list_personas(db_session, "test-account-5")
        assert len(personas) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent_persona(self, db_session):
        deleted = await delete_persona(db_session, "nonexistent-id", "test-account")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_get_active_persona(self, db_session, sample_persona_create):
        await create_persona(db_session, "test-tenant", "test-account-6", sample_persona_create)
        active = await get_active_persona(db_session, "test-account-6")
        assert active is not None
        assert active.name == "Professional Assistant"

    @pytest.mark.asyncio
    async def test_get_active_persona_none(self, db_session):
        active = await get_active_persona(db_session, "nonexistent-account")
        assert active is None


# ── Conversation Context Tests ──────────────────────────────────────────


class TestConversationContext:
    @pytest.mark.asyncio
    async def test_get_or_create_conversation(self, db_session):
        conv = await get_or_create_conversation(
            db_session, "test-tenant", "test-account", "-100test",
        )
        assert conv.id is not None
        assert conv.chat_id == "-100test"
        assert conv.messages == []
        assert conv.message_count == 0

    @pytest.mark.asyncio
    async def test_get_or_create_conversation_reuses_existing(self, db_session):
        conv1 = await get_or_create_conversation(
            db_session, "test-tenant", "test-account", "-100test2",
            chat_title="Test Group",
        )
        conv2 = await get_or_create_conversation(
            db_session, "test-tenant", "test-account", "-100test2",
        )
        assert conv1.id == conv2.id
        assert conv2.chat_title == "Test Group"

    @pytest.mark.asyncio
    async def test_add_message_to_conversation(self, db_session):
        conv = await get_or_create_conversation(
            db_session, "test-tenant", "test-account", "-100test3",
        )
        await add_message_to_conversation(db_session, conv, "user", "Hello!", message_id=1)
        await add_message_to_conversation(db_session, conv, "assistant", "Hi!", message_id=2)

        assert conv.message_count == 2
        assert len(conv.messages) == 2
        assert conv.messages[0]["role"] == "assistant"  # newest first
        assert conv.messages[0]["content"] == "Hi!"

    @pytest.mark.asyncio
    async def test_conversation_pruning(self, db_session):
        conv = await get_or_create_conversation(
            db_session, "test-tenant", "test-account", "-100prune",
        )
        # Add more messages than MAX (20)
        for i in range(25):
            await add_message_to_conversation(
                db_session, conv, "user" if i % 2 == 0 else "assistant",
                f"Message {i}", message_id=i,
            )

        assert conv.message_count <= 20
        assert len(conv.messages) <= 20


# ── Suggestion Generation Tests ─────────────────────────────────────────


class TestSuggestionGeneration:
    @pytest.mark.asyncio
    @patch("app.services.ai_reply_v2_service.call_deepseek")
    @patch("app.services.ai_reply_v2_service.search_memory")
    async def test_generate_suggestions_success(
        self, mock_search_memory, mock_call_deepseek, db_session, sample_suggestion_request,
    ):
        # Mock DeepSeek response
        mock_call_deepseek.return_value = (
            json.dumps({
                "primary": {"text": "네, 문의 감사합니다! 어떤 제품에 관심이 있으신가요?", "confidence": 0.92, "reason": "Professional greeting"},
                "alternatives": [
                    {"text": "안녕하세요! 무엇을 도와드릴까요?", "confidence": 0.75, "reason": "Friendly approach"},
                    {"text": "문의해 주셔서 감사합니다. 자세히 알려주시면 답변 드리겠습니다.", "confidence": 0.60, "reason": "Formal response"},
                ],
            }),
            150,
            None,
        )
        # Mock memory search - no context
        mock_search_memory.return_value = []

        # Create a persona first
        persona = await create_persona(
            db_session, "test-tenant", "test-account-1",
            PersonaCreate(name="Test Persona", tone="professional"),
        )

        suggestion = await generate_suggestions(db_session, "test-tenant", sample_suggestion_request)
        assert suggestion is not None
        assert suggestion.status == "pending"
        assert suggestion.suggestions["primary"]["text"] is not None
        assert suggestion.suggestions["primary"]["confidence"] == 0.92
        assert len(suggestion.suggestions["alternatives"]) == 2
        assert suggestion.response_time_ms is not None
        assert suggestion.response_time_ms > 0

    @pytest.mark.asyncio
    @patch("app.services.ai_reply_v2_service.call_deepseek")
    async def test_generate_suggestions_deepseek_failure(
        self, mock_call_deepseek, db_session, sample_suggestion_request,
    ):
        mock_call_deepseek.return_value = (None, 0, None)

        suggestion = await generate_suggestions(db_session, "test-tenant", sample_suggestion_request)
        assert suggestion is None

    @pytest.mark.asyncio
    @patch("app.services.ai_reply_v2_service.call_deepseek")
    async def test_auto_reply_high_confidence(
        self, mock_call_deepseek, db_session,
    ):
        mock_call_deepseek.return_value = (
            json.dumps({
                "primary": {"text": "Auto reply text", "confidence": 0.95, "reason": "High confidence"},
                "alternatives": [],
            }),
            100,
            None,
        )

        request = SuggestionGenerateRequest(
            account_id="test-account-auto",
            chat_id="-100auto",
            user_id="user123",
            incoming_message="Test message for auto reply",
            auto_reply_enabled=True,
        )

        suggestion = await generate_suggestions(db_session, "test-tenant", request)
        assert suggestion is not None
        assert suggestion.auto_reply_enabled is True
        assert suggestion.auto_reply_sent is True
        assert suggestion.status == "approved"
        assert suggestion.selected_suggestion == "primary"

    @pytest.mark.asyncio
    @patch("app.services.ai_reply_v2_service.call_deepseek")
    async def test_auto_reply_low_confidence(
        self, mock_call_deepseek, db_session,
    ):
        mock_call_deepseek.return_value = (
            json.dumps({
                "primary": {"text": "Low confidence reply", "confidence": 0.5, "reason": "Uncertain"},
                "alternatives": [],
            }),
            100,
            None,
        )

        request = SuggestionGenerateRequest(
            account_id="test-account-low",
            chat_id="-100low",
            user_id="user456",
            incoming_message="Test message",
            auto_reply_enabled=True,
        )

        suggestion = await generate_suggestions(db_session, "test-tenant", request)
        assert suggestion is not None
        assert suggestion.auto_reply_enabled is True
        assert suggestion.auto_reply_sent is False  # Below 0.85 threshold
        assert suggestion.status == "pending"


# ── Review Workflow Tests ───────────────────────────────────────────────


class TestReviewWorkflow:
    @pytest.mark.asyncio
    @patch("app.services.ai_reply_v2_service.call_deepseek")
    async def test_review_approve(self, mock_call_deepseek, db_session):
        mock_call_deepseek.return_value = (
            json.dumps({
                "primary": {"text": "Test reply", "confidence": 0.8, "reason": "Test"},
                "alternatives": [],
            }),
            100,
            None,
        )

        request = SuggestionGenerateRequest(
            account_id="test-account-review",
            chat_id="-100review",
            user_id="user789",
            incoming_message="Test message",
        )
        suggestion = await generate_suggestions(db_session, "test-tenant", request)
        assert suggestion is not None

        # Approve
        review = SuggestionReviewRequest(
            status="approved",
            selected_suggestion="primary",
        )
        reviewed = await review_suggestion(db_session, suggestion.id, "test-account-review", "admin", review)
        assert reviewed is not None
        assert reviewed.status == "approved"
        assert reviewed.selected_suggestion == "primary"
        assert reviewed.reviewed_by == "admin"
        assert reviewed.reviewed_at is not None

    @pytest.mark.asyncio
    @patch("app.services.ai_reply_v2_service.call_deepseek")
    async def test_review_dismiss(self, mock_call_deepseek, db_session):
        mock_call_deepseek.return_value = (
            json.dumps({
                "primary": {"text": "Test reply", "confidence": 0.8, "reason": "Test"},
                "alternatives": [],
            }),
            100,
            None,
        )

        request = SuggestionGenerateRequest(
            account_id="test-account-dismiss",
            chat_id="-100dismiss",
            user_id="user999",
            incoming_message="Test message",
        )
        suggestion = await generate_suggestions(db_session, "test-tenant", request)
        assert suggestion is not None

        # Dismiss
        review = SuggestionReviewRequest(status="dismissed")
        reviewed = await review_suggestion(db_session, suggestion.id, "test-account-dismiss", "admin", review)
        assert reviewed is not None
        assert reviewed.status == "dismissed"

    @pytest.mark.asyncio
    async def test_review_nonexistent(self, db_session):
        review = SuggestionReviewRequest(status="approved")
        result = await review_suggestion(db_session, "nonexistent", "test-account", "admin", review)
        assert result is None


# ── Feedback Tests ──────────────────────────────────────────────────────


class TestFeedback:
    @pytest.mark.asyncio
    @patch("app.services.ai_reply_v2_service.call_deepseek")
    async def test_submit_feedback(self, mock_call_deepseek, db_session):
        mock_call_deepseek.return_value = (
            json.dumps({
                "primary": {"text": "Test reply", "confidence": 0.8, "reason": "Test"},
                "alternatives": [],
            }),
            100,
            None,
        )

        request = SuggestionGenerateRequest(
            account_id="test-account-fb",
            chat_id="-100fb",
            user_id="user111",
            incoming_message="Test",
        )
        suggestion = await generate_suggestions(db_session, "test-tenant", request)
        assert suggestion is not None

        feedback = SuggestionFeedbackRequest(rating=5, comment="Excellent!", was_helpful=True)
        updated = await submit_feedback(db_session, suggestion.id, "test-account-fb", feedback)
        assert updated is not None
        assert updated.feedback is not None
        assert updated.feedback["rating"] == 5
        assert updated.feedback["comment"] == "Excellent!"
        assert updated.feedback["was_helpful"] is True

    @pytest.mark.asyncio
    async def test_submit_feedback_nonexistent(self, db_session):
        feedback = SuggestionFeedbackRequest(rating=3)
        result = await submit_feedback(db_session, "nonexistent", "test-account", feedback)
        assert result is None