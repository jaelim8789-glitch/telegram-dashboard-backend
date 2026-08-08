"""Emotion analysis service for TeleMon AI Chat.

Classifies user message emotions and suggests response tone adjustments.
Uses Ollama API for lightweight classification.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.config import settings
from app.core.logging import get_logger
from app.services.ai_core_service import call_ollama

logger = get_logger(__name__)

_EMOTION_CLASSIFIER_PROMPT = """당신은 한국어 텍스트 감정 분석기입니다. 사용자의 메시지를 분석하여 다음 중 하나의 감정으로 분류하고, 추천 응답 톤을 제시하세요.

감정 카테고리:
- positive: 기쁨, 만족, 감사, 칭찬, 흥분
- negative: 분노, 실망, 불만, 짜증, 슬픔, 걱정
- neutral: 일반 정보 요청, 평범한 대화, 사실 확인
- curious: 질문, 탐구, 학습 요청, 호기심
- urgent: 긴급 문제 해결, 즉시 필요, 시스템 장애

응답 톤 가이드:
- negative: 공감 우선, 사과/이해 표현 후 해결책 제시
- positive: 밝고 활기찬 톤, 격려 표현
- curious: 상세하고 설명적인 톤, 예시 포함
- urgent: 간결하고 직접적인 톤, 핵심만 전달
- neutral: 전문적이고 균형 잡힌 톤

입력: "{text}"

다음 JSON 형식으로만 응답하세요:
{{"emotion": "positive|negative|neutral|curious|urgent", "confidence": 0.0-1.0, "tone": "empathetic|enthusiastic|detailed|concise|professional", "reason": "간단한 분류 근거"}}
"""


async def analyze_emotion(text: str) -> dict[str, Any] | None:
    """Classify emotion in a user message.

    Args:
        text: User message content (max 10000 chars).

    Returns:
        dict with keys: emotion, confidence, tone, reason
        or None if classification fails.
    """
    if not text or not text.strip():
        return None

    prompt = _EMOTION_CLASSIFIER_PROMPT.format(text=text[:2000])

    try:
        reply, _, _ = await call_ollama(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            model=settings.ollama_model or "ollama-chat",
        )

        if not reply:
            return None

        cleaned = re.sub(r"```json\s*|\s*```", "", reply.strip())
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            logger.warning("emotion_analysis_no_json", raw=reply[:200])
            return None

        result = json.loads(match.group(0))
        emotion = result.get("emotion", "neutral")
        confidence = float(result.get("confidence", 0.5))
        tone = result.get("tone", "professional")
        reason = result.get("reason", "")

        if emotion not in ("positive", "negative", "neutral", "curious", "urgent"):
            emotion = "neutral"
        if tone not in ("empathetic", "enthusiastic", "detailed", "concise", "professional"):
            tone = "professional"
        confidence = max(0.0, min(1.0, confidence))

        logger.info(
            "emotion_analyzed",
            emotion=emotion,
            confidence=confidence,
            tone=tone,
        )

        return {
            "emotion": emotion,
            "confidence": confidence,
            "tone": tone,
            "reason": reason,
        }
    except Exception as exc:
        logger.warning("emotion_analysis_failed", error=str(exc))
        return None


_EMOTION_TONE_INJECTION = {
    "empathetic": (
        "사용자의 감정 상태: {emotion} (신뢰도 {confidence:.0%}). "
        "사용자의 감정을 먼저 공감하고 이해한다는 표현으로 답변을 시작하세요. "
        "해결책은 공감 표현 이후에 제시하세요."
    ),
    "enthusiastic": (
        "사용자의 감정 상태: {emotion} (신뢰도 {confidence:.0%}). "
        "밝고 활기찬 톤으로 응답하세요. 긍정적인 에너지를 유지하고 격려 표현을 포함하세요."
    ),
    "detailed": (
        "사용자의 감정 상태: {emotion} (신뢰도 {confidence:.0%}). "
        "호기심이 많으므로 구체적인 설명과 예시를 포함한 상세한 답변을 제공하세요. "
        "단계별로 설명하고 관련 개념을 풍부하게 다루세요."
    ),
    "concise": (
        "사용자의 감정 상태: {emotion} (신뢰도 {confidence:.0%}). "
        "긴급한 상황이므로 간결하고 직접적으로 핵심만 전달하세요. "
        "불필요한 설명은 생략하고 즉시 실행 가능한 조언을 하세요."
    ),
    "professional": (
        "사용자의 감정 상태: {emotion} (신뢰도 {confidence:.0%}). "
        "전문적이고 균형 잡힌 톤으로 응답하세요. 객관적인 정보를 우선 제공하세요."
    ),
}


def build_emotion_system_message(emotion_data: dict[str, Any]) -> str | None:
    """Build a system message for emotion-aware response generation.

    Args:
        emotion_data: Result from analyze_emotion().

    Returns:
        System message string or None if no data.
    """
    if not emotion_data:
        return None

    emotion = emotion_data.get("emotion", "neutral")
    confidence = emotion_data.get("confidence", 0.5)
    tone = emotion_data.get("tone", "professional")

    template = _EMOTION_TONE_INJECTION.get(tone, _EMOTION_TONE_INJECTION["professional"])
    return template.format(emotion=emotion, confidence=confidence)
