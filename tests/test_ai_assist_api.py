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
        ai_assist_module, "_call_deepseek", AsyncMock(return_value="안녕하세요! 문의 감사합니다.")
    )

    res = await client.post("/api/ai/suggest-reply", json={"incoming_message": "영업시간이 어떻게 되나요?"})

    assert res.status_code == 200
    assert res.json()["reply"] == "안녕하세요! 문의 감사합니다."


@pytest.mark.asyncio
async def test_suggest_reply_503_on_deepseek_failure(client, monkeypatch):
    monkeypatch.setattr(ai_assist_module, "_call_deepseek", AsyncMock(return_value=None))

    res = await client.post("/api/ai/suggest-reply", json={"incoming_message": "안녕하세요"})

    assert res.status_code == 503


@pytest.mark.asyncio
async def test_generate_broadcast_parses_valid_json_and_filters_to_candidates(client, monkeypatch):
    fake_json = (
        '{"message": "이번 주 할인 안내입니다!", '
        '"recommended_chat_ids": ["c1", "unknown-id"], '
        '"reasoning": "최근 활성 고객 대상"}'
    )
    monkeypatch.setattr(ai_assist_module, "_call_deepseek", AsyncMock(return_value=fake_json))

    res = await client.post(
        "/api/ai/generate-broadcast",
        json={
            "prompt": "할인 안내 방송문 작성해줘",
            "candidate_recipients": [{"chat_id": "c1", "name": "VIP 그룹"}, {"chat_id": "c2", "name": "일반 그룹"}],
        },
    )

    assert res.status_code == 200
    body = res.json()
    assert body["message"] == "이번 주 할인 안내입니다!"
    # "unknown-id" isn't in candidate_recipients, so it must be filtered out —
    # the model is never trusted to invent a sendable chat_id.
    assert body["recommended_chat_ids"] == ["c1"]
    assert body["reasoning"] == "최근 활성 고객 대상"


@pytest.mark.asyncio
async def test_generate_broadcast_degrades_gracefully_on_malformed_json(client, monkeypatch):
    monkeypatch.setattr(ai_assist_module, "_call_deepseek", AsyncMock(return_value="그냥 평문 응답입니다"))

    res = await client.post("/api/ai/generate-broadcast", json={"prompt": "발송 문구 작성해줘"})

    assert res.status_code == 200
    body = res.json()
    assert body["message"] == "그냥 평문 응답입니다"
    assert body["recommended_chat_ids"] == []


@pytest.mark.asyncio
async def test_generate_broadcast_persists_draft_history(client, db_session, monkeypatch):
    monkeypatch.setattr(ai_assist_module, "_call_deepseek", AsyncMock(return_value="발송 문구 초안"))

    res = await client.post("/api/ai/generate-broadcast", json={"prompt": "발송 문구 작성해줘"})
    assert res.status_code == 200

    headers = await _admin_headers(client)
    drafts_res = await client.get("/api/ai/broadcast-drafts", headers=headers)
    assert drafts_res.status_code == 200
    assert any(d["message"] == "발송 문구 초안" for d in drafts_res.json())


@pytest.mark.asyncio
async def test_generate_broadcast_rejects_too_many_candidates(client):
    too_many = [{"chat_id": f"c{i}", "name": "그룹"} for i in range(201)]
    res = await client.post(
        "/api/ai/generate-broadcast", json={"prompt": "발송 문구 작성해줘", "candidate_recipients": too_many}
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_analyze_customers_admin_requires_tenant_id(client):
    """Default `client` fixture identity is admin (see conftest.py) — admin
    must specify which tenant's leads to analyze, no whole-platform aggregate
    here (that's what ops-reports is for)."""
    res = await client.post("/api/ai/analyze-customers", json={"days": 30})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_analyze_customers_admin_with_tenant_id_queries_real_leads(client, db_session, monkeypatch):
    db_session.add(Lead(tenant_id="tenant-1", account_id="acc-1", telegram_user_id="u1", source_chat_id="c1", total_messages=5))
    await db_session.commit()

    fake_reply = "고객 현황은 안정적입니다.\n이탈 위험 증가; 최근 미접속 고객 발생"
    monkeypatch.setattr(ai_analysis_service_module, "_call_deepseek", AsyncMock(return_value=fake_reply))

    res = await client.post("/api/ai/analyze-customers", json={"tenant_id": "tenant-1", "days": 30})

    assert res.status_code == 200
    body = res.json()
    assert "안정적" in body["report"]
    assert any("이탈" in insight for insight in body["insights"])


@pytest.mark.asyncio
async def test_analyze_customers_non_admin_is_forced_to_own_tenant(client, db_session, monkeypatch):
    """A non-admin caller can never analyze another tenant's leads, even if
    they pass a different tenant_id — fail-closed, same policy as
    require_account_tenant_access elsewhere."""
    db_session.add(Lead(tenant_id="own-tenant", account_id="acc-1", telegram_user_id="u1", source_chat_id="c1", total_messages=5))
    db_session.add(Lead(tenant_id="other-tenant", account_id="acc-2", telegram_user_id="u2", source_chat_id="c2", total_messages=99))
    await db_session.commit()

    captured_prompts: list[str] = []

    async def fake_deepseek(messages):
        captured_prompts.append(messages[1]["content"])
        return "요약 리포트"

    monkeypatch.setattr(ai_analysis_service_module, "_call_deepseek", fake_deepseek)
    app.dependency_overrides[get_current_identity] = lambda: Identity(kind="user", tenant_id="own-tenant")

    res = await client.post("/api/ai/analyze-customers", json={"tenant_id": "other-tenant", "days": 30})

    assert res.status_code == 200
    # Only own-tenant's lead data should ever reach the prompt.
    assert '"total_leads": 1' in captured_prompts[0]


@pytest.mark.asyncio
async def test_ops_reports_requires_real_admin_token(client):
    """client fixture bypasses require_api_key_or_admin but NOT require_admin —
    this endpoint is admin-only because it aggregates cross-tenant data."""
    res = await client.get("/api/ai/ops-reports")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_ops_reports_lists_reports_for_real_admin(client, db_session):
    from app.models.ai_ops_report import AiOpsReport

    db_session.add(AiOpsReport(report="테스트 리포트", anomalies_json='["이상 징후 A"]'))
    await db_session.commit()

    headers = await _admin_headers(client)
    res = await client.get("/api/ai/ops-reports", headers=headers)

    assert res.status_code == 200
    body = res.json()
    assert any(r["report"] == "테스트 리포트" for r in body)
