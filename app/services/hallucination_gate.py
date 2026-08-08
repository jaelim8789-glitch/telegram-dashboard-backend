"""Real-time hallucination blocking gate for AI Chat.

Validates generated claims against retrieved KB evidence and masks
unsupported assertions before they reach the user.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.config import settings
from app.core.logging import get_logger
from app.services.ai_core_service import call_ollama

logger = get_logger(__name__)

_HALLUCINATION_CHECK_PROMPT = """당신은 AI 답변의 사실 검증기입니다. 주어진 답변과 참고 지식만을 근거로, 답변의 각 주장이 지식에 의해 뒷받침되는지 평가하세요.

규칙:
1. 답변의 **각 문장/주장**을 개별적으로 평가하세요.
2. 참고 지식에 명시적으로 있으면 "supported"
3. 참고 지식에 없지만 일반 상식이면 "common_knowledge"
4. 참고 지식에 없고 검증 불가하면 "unsupported"
5. "unsupported" 주장이 2개 이상이면 전체 답변을 "high_risk"로 표시하세요.

다음 JSON 형식으로만 응답하세요:
{{"risk": "low|medium|high", "unsupported_claims": ["주장1", "주장2"], "supported_claims": ["주장1"], "total_claims": N, "recommendation": "approved|revise|block"}}

=== 참고 지식 ===
{knowledge}

=== AI 답변 ===
{answer}
"""


async def check_hallucination_risk(answer: str, kb_context: list[str]) -> dict[str, Any]:
    """Check if the generated answer contains unsupported claims.

    Args:
        answer: Generated AI answer.
        kb_context: Retrieved KB chunks used for generation.

    Returns:
        Dict with keys: risk, unsupported_claims, supported_claims,
        total_claims, recommendation.
    """
    if not answer or not answer.strip():
        return {"risk": "low", "recommendation": "approved"}

    knowledge = "\n\n".join(kb_context[:3]) if kb_context else "(없음)"
    prompt = _HALLUCINATION_CHECK_PROMPT.format(
        knowledge=knowledge[:4000],
        answer=answer[:2000],
    )

    try:
        reply, _, _ = await call_ollama(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            model=settings.ollama_model or "ollama-chat",
        )
        if not reply:
            return {"risk": "low", "recommendation": "approved"}

        cleaned = re.sub(r"```json\s*|\s*```", "", reply.strip())
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            logger.warning("hallucination_check_no_json", raw=reply[:200])
            return {"risk": "low", "recommendation": "approved"}

        result = json.loads(match.group(0))
        risk = result.get("risk", "low")
        if risk not in ("low", "medium", "high"):
            risk = "low"
        recommendation = result.get("recommendation", "approved")
        if recommendation not in ("approved", "revise", "block"):
            recommendation = "approved"

        logger.info(
            "hallucination_checked",
            risk=risk,
            recommendation=recommendation,
            unsupported=len(result.get("unsupported_claims", [])),
        )
        return {
            "risk": risk,
            "unsupported_claims": result.get("unsupported_claims", []),
            "supported_claims": result.get("supported_claims", []),
            "total_claims": result.get("total_claims", 0),
            "recommendation": recommendation,
        }
    except Exception as exc:
        logger.warning("hallucination_check_failed", error=str(exc))
        return {"risk": "low", "recommendation": "approved"}


def mask_unsupported_claims(answer: str, unsupported_claims: list[str]) -> str:
    """Replace unsupported claims with a verification marker.

    Args:
        answer: Original answer text.
        unsupported_claims: List of unsupported claim strings.

    Returns:
        Modified answer with unsupported claims masked.
    """
    if not unsupported_claims:
        return answer
    masked = answer
    for claim in unsupported_claims:
        claim = claim.strip()
        if not claim:
            continue
        pattern = re.escape(claim[:50])
        masked = re.sub(pattern, f"[확인 필요: {claim[:30]}...]", masked, count=1)
    return masked
