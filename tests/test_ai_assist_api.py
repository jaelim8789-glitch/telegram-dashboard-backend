"""Endpoint tests for the new app.api.ai_assist routes: suggest-reply,
generate-broadcast, analyze-customers, ops-reports. generate-message and
analyze-delivery already existed and are unchanged in behavior (just
refactored internally to share app.services.ai_analysis_service).
"""

from unittest.mock import AsyncMock

import pytest

from app.config import settings
import app.api.ai_assist as ai_assist_module
import app.services.ai_analysis_service as ai_analysis_service_module
import app.services.ai_reply_service as ai_reply_service_module


async def _admin_headers(client) -> dict[str, str]:
    login = await client.post(
        "/api/admin/login", json={"username": settings.admin_username, "password": settings.admin_password}
    )
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_suggest_reply_returns_drafted_text(client, monkeypatch):
    monkeypatch.setattr(
        ai_reply_service_module, "_call_deepseek", AsyncMock(return_value="안녕하세요! 문의 감사합니다.")
    )

    res = await client.post("/api/ai/suggest-reply", json={"incoming_message": "영업시간이 어떻게 되나요?"})

    assert res.status_code == 200
    assert res.json()["reply"] == "안녕하세요! 문의 감사합니다."


@pytest.mark.asyncio
async def test_suggest_reply_503_on_deepseek_failure(client, monkeypatch):
    monkeypatch.setattr(ai_reply_service_module, "_call_deepseek", AsyncMock(return_value=None))

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
async def test_analyze_customers_returns_report_and_insights(client, monkeypatch):
    fake_reply = "고객 현황은 안정적입니다.\n이탈 위험 증가; 30일 미접속 고객 12명 발생"
    monkeypatch.setattr(ai_analysis_service_module, "_call_deepseek", AsyncMock(return_value=fake_reply))

    res = await client.post(
        "/api/ai/analyze-customers",
        json={"summary": '{"total_leads": 120}', "segments": '{"vip": 10}', "days": 30},
    )

    assert res.status_code == 200
    body = res.json()
    assert "안정적" in body["report"]
    assert any("이탈" in insight for insight in body["insights"])


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
