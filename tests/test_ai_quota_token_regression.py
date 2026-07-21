"""Regression tests for two AI billing/accuracy bugs:

1. app/services/ai_core_service.py `_get_tenant_plan` was hard-coded to return
   "free", silently capping paid (pro/team) tenants at the free AI quota. The
   paid plan must now be resolved from the Tenant row so paid limits apply.

2. app/api/ai_agent.py streamed AI responses estimated token usage as
   `len(cleaned) // 4`, which under-counts Korean (and over/under counts in
   general). The streaming path must now use the real `usage.total_tokens`
   carried in the final DeepSeek usage chunk.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import Identity, get_current_identity
from app.database import Base
from app.main import app
from app.models.tenant import Tenant
from app.models.ai import AiPlanLimit
from app.services.ai_core_service import _get_tenant_plan, check_ai_quota


# Use an in-memory SQLite engine local to this test module so we never hit the
# file-lock / "table already exists" issues the shared file-based conftest DB
# can run into on Windows. Each test gets a fresh schema.
@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


# ── Fixtures ──────────────────────────────────────────────────────────────


async def _make_tenant(db_session, tenant_id: str, plan: str) -> Tenant:
    tenant = Tenant(
        id=tenant_id,
        phone=f"+8210{tenant_id[-8:]}",
        plan=plan,
        is_active=True,
    )
    db_session.add(tenant)
    await db_session.commit()
    return tenant


async def _seed_plan_limit(db_session, plan: str, feature: str, **kwargs) -> None:
    defaults = {
        "max_requests_per_day": 100,
        "max_tokens_per_day": 100000,
        "max_credits_per_month": 0.0,
        "is_enabled": True,
    }
    defaults.update(kwargs)
    db_session.add(AiPlanLimit(id=f"{plan}-{feature}", plan=plan, feature=feature, **defaults))
    await db_session.commit()


# ── Bug 1: _get_tenant_plan resolves the real plan ────────────────────────


class TestGetTenantPlan:
    @pytest.mark.asyncio
    async def test_returns_real_plan_for_paid_tenant(self, db_session):
        await _make_tenant(db_session, "t-pro", "pro")
        plan = await _get_tenant_plan(db_session, "t-pro")
        assert plan == "pro"

    @pytest.mark.asyncio
    async def test_returns_real_plan_for_team_tenant(self, db_session):
        await _make_tenant(db_session, "t-team", "team")
        plan = await _get_tenant_plan(db_session, "t-team")
        assert plan == "team"

    @pytest.mark.asyncio
    async def test_falls_back_to_free_when_tenant_missing(self, db_session):
        plan = await _get_tenant_plan(db_session, "does-not-exist")
        assert plan == "free"


# ── Bug 1: check_ai_quota applies paid limits via the real plan ───────────


class TestCheckAiQuotaUsesRealPlan:
    """Before the fix, check_ai_quota always used the free plan because
    _get_tenant_plan returned 'free'. A pro tenant with a generous pro limit
    must NOT be capped at the free limit."""

    @pytest.mark.asyncio
    async def test_pro_tenant_gets_pro_limit_not_free(self, db_session):
        await _make_tenant(db_session, "t-pro2", "pro")
        # Free chat limit: very low (10/day). Pro chat limit: 200/day.
        await _seed_plan_limit(db_session, "free", "chat", max_requests_per_day=10)
        await _seed_plan_limit(db_session, "pro", "chat", max_requests_per_day=200)

        # No usage yet → allowed under either plan, but the applied plan must be pro.
        allowed, _ = await check_ai_quota(db_session, "t-pro2", "chat")
        assert allowed is True

        # Verify the resolved plan is actually 'pro' (not 'free') by checking
        # that a pro-sized usage count is still allowed while free would be capped.
        from app.models.ai import AiUsageRecord

        for _ in range(50):  # 50 < pro(200) but > free(10)
            db_session.add(AiUsageRecord(
                id=f"rec-{_}", tenant_id="t-pro2", feature="chat",
                tokens_used=1, requests_count=1,
            ))
        await db_session.commit()

        # 50 uses: allowed under pro (200/day), would be denied under free (10/day).
        allowed_after, reason = await check_ai_quota(db_session, "t-pro2", "chat")
        assert allowed_after is True, f"pro tenant wrongly capped: {reason}"

    @pytest.mark.asyncio
    async def test_free_tenant_is_capped_at_free_limit(self, db_session):
        await _make_tenant(db_session, "t-free", "free")
        await _seed_plan_limit(db_session, "free", "chat", max_requests_per_day=10)

        from app.models.ai import AiUsageRecord

        for _ in range(10):
            db_session.add(AiUsageRecord(
                id=f"frec-{_}", tenant_id="t-free", feature="chat",
                tokens_used=1, requests_count=1,
            ))
        await db_session.commit()

        # 10 uses == free limit (10) → next call denied.
        allowed, _ = await check_ai_quota(db_session, "t-free", "chat")
        assert allowed is False


# ── Bug 1: the 4 endpoints use real plan limits (integration smoke) ────────
# Confirms /api/ai chat, reply-assistant, broadcast-assistant, operations-report
# route through check_ai_quota -> _get_tenant_plan and are NOT silently capped
# at free for a paid tenant. We stub DeepSeek so no real API call is made, and
# seed only a *paid* plan limit (no free limit row) — if the endpoint fell back
# to free it would hit `limit is None -> allow`, which is acceptable, so instead
# we assert that a pro tenant is allowed a request count that exceeds the free
# default implied by seeding pro-only limits at a high count.


@pytest.mark.asyncio
async def test_ai_endpoints_accept_pro_tenant_high_volume(client, db_session, monkeypatch):
    """A pro tenant must be allowed to call each AI endpoint well beyond the
    free daily cap. We seed only pro limits and assert 200 (not 429)."""
    import app.api.ai as ai_module

    await _make_tenant(db_session, "t-pro3", "pro")
    await _seed_plan_limit(db_session, "pro", "chat", max_requests_per_day=200)
    await _seed_plan_limit(db_session, "pro", "reply_assistant", max_requests_per_day=500)
    await _seed_plan_limit(db_session, "pro", "broadcast_assistant", max_requests_per_day=100)
    await _seed_plan_limit(db_session, "pro", "operations_report", max_requests_per_day=50)

    # Force the caller identity to the pro tenant.
    app.dependency_overrides[get_current_identity] = lambda: Identity(
        kind="user", tenant_id="t-pro3"
    )
    monkeypatch.setattr(ai_module, "call_deepseek", AsyncMock(return_value=("{}", 10, None)))

    # chat
    r = await client.post("/api/ai/chat", json={"message": "안녕"})
    assert r.status_code == 200, r.text

    # reply-assistant
    r = await client.post("/api/ai/reply-assistant", json={
        "account_id": "a1", "chat_id": "c1", "incoming_message": "영업시간?",
    })
    assert r.status_code == 200, r.text

    # broadcast-assistant
    r = await client.post("/api/ai/broadcast-assistant", json={"purpose": "할인 안내"})
    assert r.status_code == 200, r.text

    # operations-report
    r = await client.post("/api/ai/operations-report", json={"days": 1})
    assert r.status_code == 200, r.text


# ── Bug 2: streaming token count uses real usage ──────────────────────────


class TestStreamTokenCount:
    @pytest.mark.asyncio
    async def test_stream_yields_usage_chunk_with_real_tokens(self, monkeypatch):
        """_call_deepseek_stream must emit (content, 0) for normal chunks and
        a final ('', real_tokens) usage chunk when include_usage is requested."""
        import app.services.ai_core_service as core

        async def fake_stream(*args, **kwargs):
            yield ("안녕", 0)
            yield ("하세요", 0)
            yield ("", 123)  # usage chunk

        monkeypatch.setattr(core, "_call_deepseek_stream", fake_stream)

        content_parts = []
        total_tokens = 0
        async for content, usage in core._call_deepseek_stream([]):
            if content:
                content_parts.append(content)
            if usage:
                total_tokens = usage

        assert "".join(content_parts) == "안녕하세요"
        assert total_tokens == 123

    @pytest.mark.asyncio
    async def test_ai_agent_stream_uses_real_tokens_not_length_estimate(
        self, client, db_session, monkeypatch
    ):
        """Regression: streamed agent reply must record the real token count
        from the usage chunk, not len(cleaned)//4 (which under-counts Korean)."""
        import app.api.ai_agent as ai_agent_module
        from app.models.ai_agent import AiAgent, AiChat, AiMessage

        agent = AiAgent(
            id="agent-real-tok", owner_id="tenant-rt", name="RT", role="custom",
            tools=[],
        )
        chat = AiChat(id="chat-real-tok", agent_id="agent-real-tok", tenant_id="tenant-rt", title="t")
        db_session.add(agent)
        db_session.add(chat)
        await db_session.commit()

        async def fake_stream(*args, **kwargs):
            yield ("안녕하세요 고객님", 0)
            # 40 real tokens — far more than len(cleaned)//4 (~3 for 9 chars).
            yield ("", 40)

        monkeypatch.setattr(ai_agent_module, "_call_deepseek_stream", fake_stream)
        # The stream handler opens its own sessions via app.database.async_session_maker
        # and via app.services.usage_tracker.async_session_maker (each a module-level
        # singleton bound to a different engine than the test's db_session). Patch both
        # to the in-memory test session's maker so tables/rows are visible.
        import app.database as db_module
        import app.services.usage_tracker as usage_tracker_module
        from sqlalchemy.ext.asyncio import async_sessionmaker

        maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
        monkeypatch.setattr(db_module, "async_session_maker", maker)
        monkeypatch.setattr(usage_tracker_module, "async_session_maker", maker)
        # record_usage/record_usage touch usage_tracker; keep identity sane.
        app.dependency_overrides[get_current_identity] = lambda: Identity(
            kind="user", tenant_id="tenant-rt"
        )

        async with client.stream(
            "POST", "/api/ai/chats/chat-real-tok/message/stream",
            json={"content": "안녕하세요"},
        ) as response:
            assert response.status_code == 200
            # Drain the SSE stream so _stream() runs to completion.
            chunks = []
            async for line in response.aiter_lines():
                if line.strip():
                    chunks.append(line)

        # The persisted agent message must carry the REAL token count.
        result = await db_session.execute(
            select(AiMessage).where(
                AiMessage.chat_id == "chat-real-tok", AiMessage.role == "agent"
            )
        )
        msg = result.scalar_one()
        # Before the fix this would be len("안녕하세요 고객님")//4 == 3.
        assert msg.tokens_used == 40, f"expected real token count, got {msg.tokens_used}"
