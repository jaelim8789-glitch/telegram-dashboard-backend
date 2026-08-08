"""Tests for emotion analysis service."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.emotion_analyzer import analyze_emotion, build_emotion_system_message


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_deepseek_response():
    return {
        "emotion": "negative",
        "confidence": 0.85,
        "tone": "empathetic",
        "reason": "사용자가 문제 해결에 실패하여 좌절한 것으로 보임",
    }


# ── analyze_emotion Tests ─────────────────────────────────────────────────


class TestAnalyzeEmotion:
    @pytest.mark.asyncio
    async def test_positive_emotion(self, mock_deepseek_response):
        mock_deepseek_response["emotion"] = "positive"
        mock_deepseek_response["tone"] = "enthusiastic"
        with patch("app.services.emotion_analyzer.call_ollama", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = (json.dumps(mock_deepseek_response), 50, None)
            result = await analyze_emotion("정말 감사합니다! 너무 좋아요!")
        assert result is not None
        assert result["emotion"] == "positive"
        assert result["tone"] == "enthusiastic"
        assert result["confidence"] == 0.85

    @pytest.mark.asyncio
    async def test_negative_emotion(self, mock_deepseek_response):
        with patch("app.services.emotion_analyzer.call_ollama", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = (json.dumps(mock_deepseek_response), 50, None)
            result = await analyze_emotion("왜 자꾸 오류가 나는 거죠? 정말 짜증나네요")
        assert result is not None
        assert result["emotion"] == "negative"
        assert result["tone"] == "empathetic"
        assert result["confidence"] == 0.85

    @pytest.mark.asyncio
    async def test_curious_emotion(self):
        curious_response = {
            "emotion": "curious",
            "confidence": 0.9,
            "tone": "detailed",
            "reason": "사용자가 학습 목적으로 질문함",
        }
        with patch("app.services.emotion_analyzer.call_ollama", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = (json.dumps(curious_response), 50, None)
            result = await analyze_emotion("RAG가 정확히 어떻게 작동하나요? 자세히 알려주세요")
        assert result is not None
        assert result["emotion"] == "curious"
        assert result["tone"] == "detailed"

    @pytest.mark.asyncio
    async def test_urgent_emotion(self):
        urgent_response = {
            "emotion": "urgent",
            "confidence": 0.95,
            "tone": "concise",
            "reason": "긴급한 문제 해결 요청",
        }
        with patch("app.services.emotion_analyzer.call_ollama", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = (json.dumps(urgent_response), 50, None)
            result = await analyze_emotion("서버가 다운됐어요! 빨리 고쳐주세요")
        assert result is not None
        assert result["emotion"] == "urgent"
        assert result["tone"] == "concise"

    @pytest.mark.asyncio
    async def test_empty_text_returns_none(self):
        result = await analyze_emotion("")
        assert result is None

    @pytest.mark.asyncio
    async def test_whitespace_only_returns_none(self):
        result = await analyze_emotion("   \n\t  ")
        assert result is None

    @pytest.mark.asyncio
    async def test_api_failure_returns_none(self):
        with patch("app.services.emotion_analyzer.call_ollama", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = (None, 0, None)
            result = await analyze_emotion("테스트 메시지")
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self):
        with patch("app.services.emotion_analyzer.call_ollama", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = ("이것은 JSON이 아닙니다", 50, None)
            result = await analyze_emotion("테스트 메시지")
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_emotion_fallback_to_neutral(self):
        bad_response = {
            "emotion": "unknown_emotion",
            "confidence": 0.5,
            "tone": "unknown_tone",
            "reason": "test",
        }
        with patch("app.services.emotion_analyzer.call_ollama", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = (json.dumps(bad_response), 50, None)
            result = await analyze_emotion("테스트")
        assert result is not None
        assert result["emotion"] == "neutral"
        assert result["tone"] == "professional"

    @pytest.mark.asyncio
    async def test_confidence_clamped(self):
        response = {
            "emotion": "positive",
            "confidence": 1.5,
            "tone": "enthusiastic",
            "reason": "test",
        }
        with patch("app.services.emotion_analyzer.call_ollama", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = (json.dumps(response), 50, None)
            result = await analyze_emotion("테스트")
        assert result is not None
        assert result["confidence"] == 1.0

    @pytest.mark.asyncio
    async def test_text_truncated_to_2000_chars(self):
        long_text = "A" * 5000
        with patch("app.services.emotion_analyzer.call_ollama", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = (json.dumps({"emotion": "neutral", "confidence": 0.5, "tone": "professional", "reason": ""}), 50, None)
            await analyze_emotion(long_text)
        call_arg = mock_call.call_args.kwargs["messages"][0]["content"]
        assert len(call_arg) < 3000


# ── build_emotion_system_message Tests ────────────────────────────────────


class TestBuildEmotionSystemMessage:
    def test_negative_emotion_message(self):
        result = build_emotion_system_message({
            "emotion": "negative",
            "confidence": 0.85,
            "tone": "empathetic",
        })
        assert result is not None
        assert "공감" in result
        assert "85%" in result

    def test_positive_emotion_message(self):
        result = build_emotion_system_message({
            "emotion": "positive",
            "confidence": 0.9,
            "tone": "enthusiastic",
        })
        assert result is not None
        assert "밝고 활기찬" in result

    def test_urgent_emotion_message(self):
        result = build_emotion_system_message({
            "emotion": "urgent",
            "confidence": 0.95,
            "tone": "concise",
        })
        assert result is not None
        assert "간결하고 직접적" in result

    def test_none_data_returns_none(self):
        assert build_emotion_system_message(None) is None

    def test_empty_dict_returns_none(self):
        assert build_emotion_system_message({}) is None

    def test_unknown_tone_falls_back_to_professional(self):
        result = build_emotion_system_message({
            "emotion": "neutral",
            "confidence": 0.5,
            "tone": "unknown_tone",
        })
        assert result is not None
        assert "전문적" in result
