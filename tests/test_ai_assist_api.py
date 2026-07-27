"""Endpoint tests for the new app.api.ai_assist routes: suggest-reply,
generate-broadcast, analyze-customers, ops-reports. generate-message and
analyze-delivery already existed and are unchanged in behavior (just
refactored internally to share app.services.ai_analysis_service).
"""

from unittest.mock import AsyncMock

import pytest

from app.api.deps import Identity, get_current_identity
from app.config import settings
from app.main import app
from app.models.tenant import Lead
import app.api.ai_assist as ai_assist_module
import app.services.ai_analysis_service as ai_analysis_service_module


async def _admin_headers(client) -> dict[str, str]:
    login = await client.post(
        "/api/admin/login", json={"username": settings.admin_username, "password": settings.admin_password}
    )
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_suggest_reply_returns_drafted_text(client, monkeypatch):
    monkeypatch.setattr(
        ai_assist_module, "_call_deepseek", AsyncMock(return_value="!  .")
    )

    res = await client.post("/api/ai/suggest-reply", json={"incoming_message": "  "})

    assert res.status_code == 200
    assert res.json()["reply"] == "!  ."


@pytest.mark.asyncio
async def test_suggest_reply_503_on_deepseek_failure(client, monkeypatch):
    monkeypatch.setattr(ai_assist_module, "_call_deepseek", AsyncMock(return_value=None))

    res = await client.post("/api/ai/suggest-reply", json={"incoming_message": ""})

    assert res.status_code == 503


@pytest.mark.asyncio
async def test_generate_broadcast_parses_valid_json_and_filters_to_candidates(client, monkeypatch):
    fake_json = (
        '{"message": "   !", '
        '"recommended_chat_ids": ["c1", "unknown-id"], '
        '"reasoning": "   "}'
    )
    monkeypatch.setattr(ai_assist_module, "_call_deepseek", AsyncMock(return_value=fake_json))

    res = await client.post(
        "/api/ai/generate-broadcast",
        json={
            "prompt": "   ",
            "candidate_recipients": [{"chat_id": "c1", "name": "VIP "}, {"chat_id": "c2", "name": " "}],
        },
    )

    assert res.status_code == 200
    body = res.json()
    assert body["message"] == "   !"
    # "unknown-id" isn't in candidate_recipients, so it must be filtered out 
    # the model is never trusted to invent a sendable chat_id.
    assert body["recommended_chat_ids"] == ["c1"]
    assert body["reasoning"] == "   "


@pytest.mark.asyncio
async def test_generate_broadcast_degrades_gracefully_on_malformed_json(client, monkeypatch):
    monkeypatch.setattr(ai_assist_module, "_call_deepseek", AsyncMock(return_value="  "))

    res = await client.post("/api/ai/generate-broadcast", json={"prompt": "  "})

    assert res.status_code == 200
    body = res.json()
    assert body["message"] == "  "
    assert body["recommended_chat_ids"] == []


@pytest.mark.asyncio
async def test_generate_broadcast_persists_draft_history(client, db_session, monkeypatch):
    monkeypatch.setattr(ai_assist_module, "_call_deepseek", AsyncMock(return_value="  "))

    res = await client.post("/api/ai/generate-broadcast", json={"prompt": "  "})
    assert res.status_code == 200

    headers = await _admin_headers(client)
    drafts_res = await client.get("/api/ai/broadcast-drafts", headers=headers)
    assert drafts_res.status_code == 200
    assert any(d["message"] == "  " for d in drafts_res.json())


@pytest.mark.asyncio
async def test_generate_broadcast_rejects_too_many_candidates(client):
    too_many = [{"chat_id": f"c{i}", "name": ""} for i in range(201)]
    res = await client.post(
        "/api/ai/generate-broadcast", json={"prompt": "  ", "candidate_recipients": too_many}
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_analyze_customers_admin_requires_tenant_id(client):
    """Default `client` fixture identity is admin (see conftest.py)  admin
    must specify which tenant's leads to analyze, no whole-platform aggregate
    here (that's what ops-reports is for)."""
    res = await client.post("/api/ai/analyze-customers", json={"days": 30})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_analyze_customers_admin_with_tenant_id_queries_real_leads(client, db_session, monkeypatch):
    db_session.add(Lead(tenant_id="tenant-1", account_id="acc-1", telegram_user_id="u1", source_chat_id="c1", total_messages=5))
    await db_session.commit()

    fake_reply = "  .\n  ;    "
    monkeypatch.setattr(ai_analysis_service_module, "_call_deepseek", AsyncMock(return_value=fake_reply))

    res = await client.post("/api/ai/analyze-customers", json={"tenant_id": "tenant-1", "days": 30})

    assert res.status_code == 200
    body = res.json()
    assert "" in body["report"]
    assert any("" in insight for insight in body["insights"])


@pytest.mark.asyncio
async def test_analyze_customers_non_admin_is_forced_to_own_tenant(client, db_session, monkeypatch):
    """A non-admin caller can never analyze another tenant's leads, even if
    they pass a different tenant_id  fail-closed, same policy as
    require_account_tenant_access elsewhere."""
    db_session.add(Lead(tenant_id="own-tenant", account_id="acc-1", telegram_user_id="u1", source_chat_id="c1", total_messages=5))
    db_session.add(Lead(tenant_id="other-tenant", account_id="acc-2", telegram_user_id="u2", source_chat_id="c2", total_messages=99))
    await db_session.commit()

    captured_prompts: list[str] = []

    async def fake_deepseek(messages):
        captured_prompts.append(messages[1]["content"])
        return " "

    monkeypatch.setattr(ai_analysis_service_module, "_call_deepseek", fake_deepseek)
    app.dependency_overrides[get_current_identity] = lambda: Identity(kind="user", tenant_id="own-tenant")

    res = await client.post("/api/ai/analyze-customers", json={"tenant_id": "other-tenant", "days": 30})

    assert res.status_code == 200
    # Only own-tenant's lead data should ever reach the prompt.
    assert '"total_leads": 1' in captured_prompts[0]


@pytest.mark.asyncio
async def test_ops_reports_requires_real_admin_token(client):
    """client fixture bypasses require_api_key_or_admin but NOT require_admin 
    this endpoint is admin-only because it aggregates cross-tenant data."""
    res = await client.get("/api/ai/ops-reports")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_ops_reports_lists_reports_for_real_admin(client, db_session):
    from app.models.ai_ops_report import AiOpsReport

    db_session.add(AiOpsReport(report=" ", anomalies_json='["  A"]'))
    await db_session.commit()

    headers = await _admin_headers(client)
    res = await client.get("/api/ai/ops-reports", headers=headers)

    assert res.status_code == 200
    body = res.json()
    assert any(r["report"] == " " for r in body)
