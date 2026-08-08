"""Tests for hallucination gate service."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.hallucination_gate import check_hallucination_risk, mask_unsupported_claims


class TestCheckHallucinationRisk:
    @pytest.mark.asyncio
    async def test_empty_answer_returns_low_risk(self):
        result = await check_hallucination_risk("", ["some kb context"])
        assert result["risk"] == "low"
        assert result["recommendation"] == "approved"

    @pytest.mark.asyncio
    async def test_no_json_response_returns_low_risk(self):
        with patch("app.services.hallucination_gate.call_ollama", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = ("not json", 10, None)
            result = await check_hallucination_risk("some answer", ["kb context"])
        assert result["risk"] == "low"
        assert result["recommendation"] == "approved"

    @pytest.mark.asyncio
    async def test_high_risk_blocks_answer(self):
        response = {
            "risk": "high",
            "unsupported_claims": ["사실이 아닌 주장", "또 다른 거짓"],
            "supported_claims": ["사실 주장"],
            "total_claims": 3,
            "recommendation": "block",
        }
        with patch("app.services.hallucination_gate.call_ollama", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = (json.dumps(response), 50, None)
            result = await check_hallucination_risk("answer", ["kb context"])
        assert result["risk"] == "high"
        assert result["recommendation"] == "block"

    @pytest.mark.asyncio
    async def test_unsupported_claims_masked(self):
        response = {
            "risk": "medium",
            "unsupported_claims": ["잘못된 수치 100억"],
            "supported_claims": [],
            "total_claims": 1,
            "recommendation": "revise",
        }
        with patch("app.services.hallucination_gate.call_ollama", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = (json.dumps(response), 50, None)
            result = await check_hallucination_risk("정확한 수치는 잘못된 수치 100억입니다.", ["kb context"])
        assert result["recommendation"] == "revise"
        masked = mask_unsupported_claims("정확한 수치는 잘못된 수치 100억입니다.", result["unsupported_claims"])
        assert "확인 필요" in masked


class TestMaskUnsupportedClaims:
    def test_no_claims_returns_original(self):
        assert mask_unsupported_claims("원본 텍스트", []) == "원본 텍스트"

    def test_empty_claims_returns_original(self):
        assert mask_unsupported_claims("원본 텍스트", [""]) == "원본 텍스트"

    def test_masks_first_occurrence(self):
        result = mask_unsupported_claims("이것은 잘못된 수치 100억입니다.", ["잘못된 수치 100억"])
        assert "확인 필요" in result
