"""Tests for AI Chat 2.0."""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.ai_chat_v2 import AiChatSession, AiChatMessageV2, AiChatPromptTemplate
from app.schemas.ai_chat_v2 import (
    SessionCreate,
    SessionUpdate,
    PromptTemplateCreate,
    SearchRequest,
)
from app.services.ai_chat_v2_service import (
    create_session,
    update_session,
    get_session,
    list_sessions,
    delete_session,
    get_session_messages,
    create_template,
    get_default_template,
    _apply_template,
    search_conversations,
    get_usage_stats,
    submit_message_feedback,
)


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def sample_session():
    return SessionCreate(
        title="Test Chat Session",
        model="deepseek-chat",
        tags=["test", "ai-chat"],
        metadata={"source": "pytest"},
        source="web_app",
    )


@pytest.fixture
def sample_template():
    return PromptTemplateCreate(
        name="Support Agent",
        description="Friendly support agent template",
        role="system",
        content=(
            "You are a friendly support agent for {{company_name}}. "
            "Always be helpful and polite. Answer in {{language}}."
        ),
        variables=["company_name", "language"],
        is_default=True,
    )


# ── _apply_template Tests ───────────────────────────────────────────────


class TestApplyTemplate:
    def test_no_variables(self):
        result = _apply_template("Hello, world!", {})
        assert result == "Hello, world!"

    def test_simple_substitution(self):
        result = _apply_template("Hello {{name}}!", {"name": "World"})
        assert result == "Hello World!"

    def test_multiple_variables(self):
        result = _apply_template(
            "{{greeting}} {{name}}, welcome to {{place}}!",
            {"greeting": "Hi", "name": "Alice", "place": "TeleMon"},
        )
        assert result == "Hi Alice, welcome to TeleMon!"

    def test_missing_variable_keeps_placeholder(self):
        result = _apply_template("Hello {{name}}!", {})
        assert result == "Hello {{name}}!"

    def test_empty_variables_dict(self):
        result = _apply_template("No variables here", {})
        assert result == "No variables here"


# ── Session CRUD Tests ──────────────────────────────────────────────────


class TestSessionCRUD:
    @pytest.mark.asyncio
    async def test_create_session(self, db_session, sample_session):
        session = await create_session(db_session, "test-tenant", sample_session)
        assert session.id is not None
        assert session.title == "Test Chat Session"
        assert session.model == "deepseek-chat"
        assert session.tags == ["test", "ai-chat"]
        assert session.source == "web_app"
        assert session.is_archived is False
        assert session.message_count == 0
        assert session.total_tokens == 0

    @pytest.mark.asyncio
    async def test_get_session(self, db_session, sample_session):
        created = await create_session(db_session, "test-tenant", sample_session)
        fetched = await get_session(db_session, created.id, "test-tenant")
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.title == "Test Chat Session"

    @pytest.mark.asyncio
    async def test_get_session_wrong_tenant(self, db_session, sample_session):
        created = await create_session(db_session, "test-tenant", sample_session)
        fetched = await get_session(db_session, created.id, "wrong-tenant")
        assert fetched is None

    @pytest.mark.asyncio
    async def test_list_sessions(self, db_session, sample_session):
        await create_session(db_session, "test-tenant", sample_session)
        await create_session(db_session, "test-tenant", SessionCreate(title="Session 2"))
        await create_session(db_session, "other-tenant", SessionCreate(title="Other"))

        sessions = await list_sessions(db_session, "test-tenant")
        assert len(sessions) == 2

    @pytest.mark.asyncio
    async def test_update_session(self, db_session, sample_session):
        created = await create_session(db_session, "test-tenant", sample_session)
        update = SessionUpdate(title="Updated Title", tags=["updated"])
        updated = await update_session(db_session, created.id, "test-tenant", update)
        assert updated is not None
        assert updated.title == "Updated Title"
        assert updated.tags == ["updated"]

    @pytest.mark.asyncio
    async def test_delete_session_archives(self, db_session, sample_session):
        created = await create_session(db_session, "test-tenant", sample_session)
        deleted = await delete_session(db_session, created.id, "test-tenant")
        assert deleted is True

        # Should not appear in normal list
        sessions = await list_sessions(db_session, "test-tenant")
        assert len(sessions) == 0

        # Should appear with include_archived
        sessions = await list_sessions(db_session, "test-tenant", include_archived=True)
        assert len(sessions) == 1
        assert sessions[0].is_archived is True

    @pytest.mark.asyncio
    async def test_delete_nonexistent_session(self, db_session):
        deleted = await delete_session(db_session, "nonexistent", "test-tenant")
        assert deleted is False


# ── Message Tests ───────────────────────────────────────────────────────


class TestMessages:
    @pytest.mark.asyncio
    async def test_get_session_messages_empty(self, db_session, sample_session):
        session = await create_session(db_session, "test-tenant", sample_session)
        messages = await get_session_messages(db_session, session.id, "test-tenant")
        assert messages == []

    @pytest.mark.asyncio
    async def test_get_session_messages_with_data(self, db_session, sample_session):
        session = await create_session(db_session, "test-tenant", sample_session)

        # Add messages directly
        for i, role in enumerate(["user", "assistant"]):
            msg = AiChatMessageV2(
                id=str(uuid.uuid4()),
                session_id=session.id,
                tenant_id="test-tenant",
                role=role,
                content=f"Test message {i}",
                tokens_prompt=10,
                tokens_completion=20,
                latency_ms=100,
            )
            db_session.add(msg)
        await db_session.commit()

        messages = await get_session_messages(db_session, session.id, "test-tenant")
        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"


# ── Prompt Template Tests ───────────────────────────────────────────────


class TestPromptTemplates:
    @pytest.mark.asyncio
    async def test_create_template(self, db_session, sample_template):
        template = await create_template(db_session, "test-tenant", sample_template)
        assert template.id is not None
        assert template.name == "Support Agent"
        assert template.role == "system"
        assert template.is_default is True
        assert "{{company_name}}" in template.content

    @pytest.mark.asyncio
    async def test_get_default_template(self, db_session, sample_template):
        await create_template(db_session, "test-tenant", sample_template)
        default = await get_default_template(db_session, "test-tenant", "system")
        assert default is not None
        assert default.name == "Support Agent"

    @pytest.mark.asyncio
    async def test_get_default_template_no_default(self, db_session):
        default = await get_default_template(db_session, "test-tenant", "system")
        assert default is None

    @pytest.mark.asyncio
    async def test_create_template_only_one_default(self, db_session, sample_template):
        t1 = await create_template(db_session, "test-tenant", sample_template)
        t2_data = PromptTemplateCreate(
            name="Second Template",
            role="system",
            content="You are {{role}}.",
            variables=["role"],
            is_default=True,
        )
        t2 = await create_template(db_session, "test-tenant", t2_data)

        # t1 should no longer be default
        assert t1.is_default is True  # Original object still has True
        # But querying should return t2
        default = await get_default_template(db_session, "test-tenant", "system")
        assert default.id == t2.id


# ── Search Tests ────────────────────────────────────────────────────────


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_empty(self, db_session):
        request = SearchRequest(query="nonexistent")
        result = await search_conversations(db_session, "test-tenant", request)
        assert result.total == 0
        assert result.results == []

    @pytest.mark.asyncio
    async def test_search_finds_messages(self, db_session):
        session = AiChatSession(
            id=str(uuid.uuid4()),
            tenant_id="test-tenant",
            title="Search Test Session",
        )
        db_session.add(session)

        msg = AiChatMessageV2(
            id=str(uuid.uuid4()),
            session_id=session.id,
            tenant_id="test-tenant",
            role="user",
            content="I need help with broadcast delivery",
        )
        db_session.add(msg)
        await db_session.commit()

        request = SearchRequest(query="broadcast")
        result = await search_conversations(db_session, "test-tenant", request)
        assert result.total == 1
        assert result.results[0].session_title == "Search Test Session"
        assert "broadcast" in result.results[0].content


# ── Usage Stats Tests ───────────────────────────────────────────────────


class TestUsageStats:
    @pytest.mark.asyncio
    async def test_usage_stats_empty(self, db_session):
        stats = await get_usage_stats(db_session, "test-tenant")
        assert stats.total_sessions == 0
        assert stats.total_messages == 0
        assert stats.total_tokens == 0
        assert stats.avg_latency_ms == 0.0

    @pytest.mark.asyncio
    async def test_usage_stats_with_data(self, db_session):
        session = AiChatSession(
            id=str(uuid.uuid4()),
            tenant_id="test-tenant",
            title="Stats Test",
            message_count=5,
            total_tokens=1000,
        )
        db_session.add(session)

        msg = AiChatMessageV2(
            id=str(uuid.uuid4()),
            session_id=session.id,
            tenant_id="test-tenant",
            role="assistant",
            content="Test",
            latency_ms=200,
        )
        db_session.add(msg)
        await db_session.commit()

        stats = await get_usage_stats(db_session, "test-tenant")
        assert stats.total_sessions == 1
        assert stats.total_messages == 5
        assert stats.total_tokens == 1000
        assert stats.avg_latency_ms == 200.0


# ── Feedback Tests ──────────────────────────────────────────────────────


class TestFeedback:
    @pytest.mark.asyncio
    async def test_submit_feedback(self, db_session):
        session = AiChatSession(
            id=str(uuid.uuid4()),
            tenant_id="test-tenant",
            title="Feedback Test",
        )
        db_session.add(session)

        msg = AiChatMessageV2(
            id=str(uuid.uuid4()),
            session_id=session.id,
            tenant_id="test-tenant",
            role="assistant",
            content="Test reply",
        )
        db_session.add(msg)
        await db_session.commit()

        updated = await submit_message_feedback(db_session, msg.id, "test-tenant", 5, "Great!")
        assert updated is not None
        assert updated.feedback_score == 5
        assert updated.feedback_comment == "Great!"

    @pytest.mark.asyncio
    async def test_submit_feedback_nonexistent(self, db_session):
        result = await submit_message_feedback(db_session, "nonexistent", "test-tenant", 3)
        assert result is None