"""Tests for search quality monitor service."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.search_quality_monitor import evaluate_search_quality, _ALERT_COOLDOWN_MINUTES


class TestEvaluateSearchQuality:
    @pytest.mark.asyncio
    async def test_high_kb_score_no_alert(self):
        db = AsyncMock()
        kb_results = [AsyncMock(score=0.9)]
        result = await evaluate_search_quality(db, kb_results, None, "query", "tenant-1")
        assert result["alert_triggered"] is False
        assert result["source_used"] == "kb"

    @pytest.mark.asyncio
    async def test_low_kb_with_web_fallback_no_alert(self):
        db = AsyncMock()
        kb_results = [AsyncMock(score=0.2)]
        web_results = [{"score": 0.5, "content": "web result"}]
        result = await evaluate_search_quality(db, kb_results, web_results, "query", "tenant-1")
        assert result["alert_triggered"] is False
        assert result["source_used"] == "web"

    @pytest.mark.asyncio
    async def test_no_results_triggers_alert(self):
        db = AsyncMock()
        result = await evaluate_search_quality(db, [], None, "query", "tenant-1")
        assert result["alert_triggered"] is True
        assert result["source_used"] == "none"
