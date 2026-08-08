"""Tests for satisfaction predictor service."""

import pytest

from app.services.satisfaction_predictor import predict_satisfaction


class TestPredictSatisfaction:
    def test_positive_emotion_boosts_score(self):
        result = predict_satisfaction(
            emotion_data={"emotion": "positive", "confidence": 0.9, "tone": "enthusiastic"},
            quality_metrics={"final": 90},
            kb_confidence=0.8,
            answer="네, 맞습니다. 파이썬은 높은 수준의 프로그래밍 언어입니다. " * 5,
        )
        assert result["score"] >= 0.6
        assert result["risk_level"] == "low"

    def test_negative_emotion_lowers_score(self):
        result = predict_satisfaction(
            emotion_data={"emotion": "negative", "confidence": 0.9, "tone": "empathetic"},
            quality_metrics={"final": 50},
            kb_confidence=0.3,
            answer="짧은 답변",
        )
        assert result["score"] <= 0.5
        assert result["risk_level"] in ("medium", "high")

    def test_urgent_emotion_lowers_score(self):
        result = predict_satisfaction(
            emotion_data={"emotion": "urgent", "confidence": 0.9, "tone": "concise"},
            quality_metrics={"final": 70},
            kb_confidence=0.6,
            answer=" " * 20,
        )
        assert result["score"] < 0.6
        assert "user_urgent_emotion" in result["factors"]

    def test_empty_answer_lowers_score(self):
        result = predict_satisfaction(
            emotion_data=None,
            quality_metrics=None,
            kb_confidence=0.0,
            answer="",
        )
        assert result["score"] < 0.4
        assert "empty_answer" in result["factors"]

    def test_short_answer_lowers_score(self):
        result = predict_satisfaction(
            emotion_data=None,
            quality_metrics=None,
            kb_confidence=0.7,
            answer="짧음",
        )
        assert "answer_short" in result["factors"]

    def test_refusal_detected_lowers_score(self):
        result = predict_satisfaction(
            emotion_data=None,
            quality_metrics=None,
            kb_confidence=0.5,
            answer="죄송합니다. 도와드릴 수 없습니다.",
        )
        assert "refusal_detected" in result["factors"]

    def test_high_quality_boosts_score(self):
        result = predict_satisfaction(
            emotion_data={"emotion": "positive", "confidence": 0.8, "tone": "enthusiastic"},
            quality_metrics={"final": 95},
            kb_confidence=0.9,
            answer="상세하고 유용한 답변입니다. 추가 정보도 제공드립니다. " * 5,
        )
        assert result["score"] >= 0.7
        assert "high_quality_score" in result["factors"]

    def test_tool_calls_boost_score(self):
        result = predict_satisfaction(
            emotion_data=None,
            quality_metrics={"final": 75},
            kb_confidence=0.7,
            answer="툴 실행 결과입니다. 요청하신 작업을 완료했습니다. " * 5,
            has_tool_calls=True,
        )
        assert "tool_executed" in result["factors"]
