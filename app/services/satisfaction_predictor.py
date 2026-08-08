"""User satisfaction prediction for AI Chat.

Predicts the likelihood of negative user feedback (score <= 2)
before the user actually submits it, enabling proactive intervention.
"""

from __future__ import annotations

import json
import math
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Feature weights for lightweight scoring (no model training required).
# Tuned heuristically from existing feature importance in feedback data.
_FEATURE_WEIGHTS = {
    "emotion_negative": 0.35,
    "emotion_urgent": 0.25,
    "quality_low": 0.25,
    "kb_confidence_low": 0.15,
    "answer_short": 0.10,
    "refusal": 0.20,
    "rambling": 0.05,
    "emotion_positive": 0.20,
    "quality_high": 0.20,
    "kb_confidence_high": 0.10,
}
_BIAS = 0.55
_THRESHOLD = 0.40


def predict_satisfaction(
    emotion_data: dict[str, Any] | None,
    quality_metrics: dict[str, Any] | None,
    kb_confidence: float,
    answer: str,
    has_tool_calls: bool = False,
) -> dict[str, Any]:
    """Predict user satisfaction score (0.0-1.0, higher = more satisfied).

    Args:
        emotion_data: Result from analyze_emotion().
        quality_metrics: Result from _quality_score().
        kb_confidence: Max KB search confidence (0.0-1.0).
        answer: Generated answer text.
        has_tool_calls: Whether the response involved tool execution.

    Returns:
        Dict with keys: score (0-1), risk_level (low/medium/high),
        factors (list of contributing features).
    """
    score = _BIAS
    factors: list[str] = []

    if not answer or not answer.strip():
        score -= 0.30
        factors.append("empty_answer")

    alen = len(answer or "")
    if alen < 40:
        score -= _FEATURE_WEIGHTS["answer_short"]
        factors.append("answer_short")

    # Emotion signals
    if emotion_data:
        emotion = emotion_data.get("emotion", "neutral")
        if emotion == "negative":
            score -= _FEATURE_WEIGHTS["emotion_negative"]
            factors.append("user_negative_emotion")
        elif emotion == "urgent":
            score -= _FEATURE_WEIGHTS["emotion_urgent"]
            factors.append("user_urgent_emotion")
        elif emotion == "positive":
            score += _FEATURE_WEIGHTS["emotion_positive"]
            factors.append("user_positive_emotion")

    # Quality metrics
    if quality_metrics:
        final_q = quality_metrics.get("final", 70)
        if final_q < 60:
            score -= _FEATURE_WEIGHTS["quality_low"]
            factors.append("low_quality_score")
        elif final_q >= 85:
            score += _FEATURE_WEIGHTS["quality_high"]
            factors.append("high_quality_score")

    # KB confidence
    if kb_confidence < 0.5:
        score -= _FEATURE_WEIGHTS["kb_confidence_low"]
        factors.append("low_kb_confidence")
    elif kb_confidence >= 0.8:
        score += _FEATURE_WEIGHTS["kb_confidence_high"]
        factors.append("high_kb_confidence")

    # Refusal / rambling
    low = (answer or "").lower()
    _refusal_hints = ("죄송", "sorry", "can't help", "도와드릴 수 없")
    _rambling = False
    if alen >= 60:
        frags = [f.strip() for f in __import__("re").split(r'[.!?\n]', low) if len(f.strip()) >= 8]
        if len(frags) >= 4:
            seen: dict[str, int] = {}
            for f in frags:
                key = f[:12]
                seen[key] = seen.get(key, 0) + 1
            extras = sum(c - 1 for c in seen.values())
            _rambling = extras / len(frags) > 0.3

    if any(h in low for h in _refusal_hints):
        score -= _FEATURE_WEIGHTS["refusal"]
        factors.append("refusal_detected")
    if _rambling:
        score -= _FEATURE_WEIGHTS["rambling"]
        factors.append("rambling_detected")

    # Tool calls tend to increase satisfaction (concrete action)
    if has_tool_calls:
        score += 0.05
        factors.append("tool_executed")

    score = max(0.0, min(1.0, score))

    if score >= 0.65:
        risk = "low"
    elif score >= _THRESHOLD:
        risk = "medium"
    else:
        risk = "high"

    logger.info(
        "satisfaction_predicted",
        score=round(score, 3),
        risk=risk,
        factors=factors,
    )
    return {
        "score": round(score, 3),
        "risk_level": risk,
        "factors": factors,
    }
