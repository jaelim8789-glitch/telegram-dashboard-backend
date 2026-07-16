"""Tests for app.services.ai_ops_service — the periodic "AI 운영 자동화" job.
Report-only: verifies it stores a row and takes no other action. Mocks the
delivery_analytics gathering functions directly (those have their own tests
elsewhere) and the DeepSeek call, so this stays focused on ai_ops_service's
own prompt-building + storage logic.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.services.ai_analysis_service as ai_analysis_service_module
import app.services.ai_ops_service as ai_ops_service_module
from app.models.ai_ops_report import AiOpsReport
from app.services.delivery_analytics import AccountPerformanceItem, FailureBreakdownItem, SummaryResult
from app.services.ai_ops_service import generate_and_store_ops_report


class db_session_cm:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _patch_gatherers(monkeypatch):
    monkeypatch.setattr(
        ai_ops_service_module, "get_summary", AsyncMock(return_value=SummaryResult(total_attempted=100, successful=90, failed=10, success_rate=90.0))
    )
    monkeypatch.setattr(
        ai_ops_service_module,
        "get_failure_breakdown",
        AsyncMock(return_value=[FailureBreakdownItem(status="flood_wait", count=10)]),
    )
    monkeypatch.setattr(
        ai_ops_service_module,
        "get_account_performance",
        AsyncMock(return_value=[AccountPerformanceItem(account_id="acc-1", attempted=100, successful=90, failed=10, success_rate=90.0)]),
    )


@pytest.mark.asyncio
async def test_generate_and_store_ops_report_persists_report(db_session, monkeypatch):
    _patch_gatherers(monkeypatch)
    monkeypatch.setattr(ai_ops_service_module, "async_session_maker", lambda: db_session_cm(db_session))

    fake_deepseek = AsyncMock(
        return_value="전반적으로 안정적입니다.\nflood_wait 이상 증가; 3계정에서 발생, 쿨다운 조정 필요"
    )
    monkeypatch.setattr(ai_analysis_service_module, "_call_deepseek", fake_deepseek)

    returned = await generate_and_store_ops_report()

    result = await db_session.execute(select(AiOpsReport))
    rows = result.scalars().all()
    assert len(rows) == 1
    assert "안정적" in rows[0].report
    assert "flood_wait" in rows[0].anomalies_json
    assert returned is not None
    assert returned.id == rows[0].id


@pytest.mark.asyncio
async def test_generate_and_store_ops_report_returns_none_and_skips_storage_on_data_gathering_failure(
    db_session, monkeypatch
):
    monkeypatch.setattr(ai_ops_service_module, "get_summary", AsyncMock(side_effect=RuntimeError("db down")))
    monkeypatch.setattr(ai_ops_service_module, "async_session_maker", lambda: db_session_cm(db_session))

    returned = await generate_and_store_ops_report()

    assert returned is None
    result = await db_session.execute(select(AiOpsReport))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_generate_and_store_ops_report_skips_when_already_in_progress(db_session, monkeypatch):
    _patch_gatherers(monkeypatch)
    monkeypatch.setattr(ai_ops_service_module, "async_session_maker", lambda: db_session_cm(db_session))
    monkeypatch.setattr(ai_analysis_service_module, "_call_deepseek", AsyncMock(return_value="리포트"))
    monkeypatch.setattr(ai_ops_service_module, "_generating", True)

    returned = await generate_and_store_ops_report()

    assert returned is None
    result = await db_session.execute(select(AiOpsReport))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_generate_and_store_ops_report_skips_storage_on_deepseek_failure(db_session, monkeypatch):
    _patch_gatherers(monkeypatch)
    monkeypatch.setattr(ai_ops_service_module, "async_session_maker", lambda: db_session_cm(db_session))
    monkeypatch.setattr(ai_analysis_service_module, "_call_deepseek", AsyncMock(return_value=None))

    await generate_and_store_ops_report()

    result = await db_session.execute(select(AiOpsReport))
    assert result.scalars().all() == []
