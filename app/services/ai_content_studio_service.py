"""
AI Content Studio Service — 6 prompt templates + DeepSeek integration.

Reuses ``call_deepseek`` from ``ai_core_service`` so the same DeepSeek
configuration, quota model, and usage tracking apply.
"""

from __future__ import annotations

import random

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.services.ai_core_service import call_deepseek, record_ai_usage

logger = get_logger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────

CONTENT_TYPES = [
    "promotional",
    "announcement",
    "engagement",
    "informational",
    "testimonial",
    "event",
]

TONES = ["short", "emotional", "intense"]

FEATURE_CONTENT_STUDIO = "ai_content_studio"

# ─── Prompt Templates ─────────────────────────────────────────────────────

_TEMPLATES: dict[str, dict[str, str]] = {
    "promotional": {
        "short": (
            "너는 텔레그램 홍보 카피라이터야. "
            "{topic}에 대한 짧고 강렬한 홍보 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 50자 이내로 작성\n"
            "- 첫 문장에 강력한 후킹\n"
            "- 행동 유도(CTA) 포함\n"
            "- 한국어로 작성"
        ),
        "emotional": (
            "너는 감성 마케팅 전문가야. "
            "{topic}에 대한 감성적인 홍보 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 공감대를 형성하는 스토리텔링\n"
            "- 감정적 어휘 활용 (따뜻함, 설렘, 공감 등)\n"
            "- 100~150자 내외\n"
            "- 한국어로 작성"
        ),
        "intense": (
            "너는 공격적인 세일즈 카피라이터야. "
            "{topic}에 대한 강렬한 홍보 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 긴급성과 희소성 강조\n"
            "- 수치와 구체적 혜택 포함\n"
            "- 강력한 CTA (지금 바로, 오늘만 등)\n"
            "- 80~120자 내외\n"
            "- 한국어로 작성"
        ),
    },
    "announcement": {
        "short": (
            "너는 공지사항 작성 전문가야. "
            "{topic}에 대한 짧고 명확한 공지 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 60자 이내\n"
            "- 핵심 정보만 전달\n"
            "- 날짜/시간/장소 등 필수 정보 포함\n"
            "- 한국어로 작성"
        ),
        "emotional": (
            "너는 커뮤니티 공지 전문가야. "
            "{topic}에 대한 따뜻한 공지 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 공동체 의식 고취\n"
            "- 친근한 톤\n"
            "- 100자 내외\n"
            "- 한국어로 작성"
        ),
        "intense": (
            "너는 긴급 공지 전문가야. "
            "{topic}에 대한 강렬하고 즉각적인 공지 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 긴급성 명확히 전달\n"
            "- 필요한 즉시 액션 요구\n"
            "- 80자 내외\n"
            "- 한국어로 작성"
        ),
    },
    "engagement": {
        "short": (
            "너는 커뮤니티 참여 유도 전문가야. "
            "{topic}에 대한 짧은 참여 유도 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 질문 or 투표 or 댓글 유도\n"
            "- 50자 이내\n"
            "- 참여 보상/혜택 언급\n"
            "- 한국어로 작성"
        ),
        "emotional": (
            "너는 커뮤니티 감동 전문가야. "
            "{topic}에 대한 감성적인 참여 유도 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 공감 스토리로 시작\n"
            "- 참여를 통한 연결감 강조\n"
            "- 100~150자 내외\n"
            "- 한국어로 작성"
        ),
        "intense": (
            "너는 바이럴 참여 유도 전문가야. "
            "{topic}에 대한 강렬한 참여 유도 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 경쟁/한정 이벤트 요소\n"
            "- FOMO (놓치면 후회) 심리 자극\n"
            "- 80~120자 내외\n"
            "- 한국어로 작성"
        ),
    },
    "informational": {
        "short": (
            "너는 정보 전달 전문가야. "
            "{topic}에 대한 짧고 요약된 정보 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 핵심 포인트 3가지\n"
            "- 80자 이내\n"
            "- 불렛포인트 형식 가능\n"
            "- 한국어로 작성"
        ),
        "emotional": (
            "너는 스토리텔링 정보 전달자야. "
            "{topic}에 대한 감성적인 정보 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 스토리로 정보 감싸기\n"
            "- 실용성 + 감동\n"
            "- 120~180자 내외\n"
            "- 한국어로 작성"
        ),
        "intense": (
            "너는 충격적 정보 전달자야. "
            "{topic}에 대한 강렬한 정보 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 놀라운 사실/수치로 시작\n"
            "- 즉시 행동 유도\n"
            "- 100~150자 내외\n"
            "- 한국어로 작성"
        ),
    },
    "testimonial": {
        "short": (
            "너는 고객 후기 작성 전문가야. "
            "{topic}에 대한 짧은 후기 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 실제 사용자 톤\n"
            "- 구체적 효과/결과 언급\n"
            "- 70자 이내\n"
            "- 한국어로 작성"
        ),
        "emotional": (
            "너는 감동적인 고객 스토리 작가야. "
            "{topic}에 대한 감성적인 후기/리뷰 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- Before/After 스토리\n"
            "- 진심 어린 감동 표현\n"
            "- 120~180자 내외\n"
            "- 한국어로 작성"
        ),
        "intense": (
            "너는 바이럴 후기 카피라이터야. "
            "{topic}에 대한 강렬한 후기/리뷰 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 극적인 변화 강조\n"
            "- 구체적 수치/결과\n"
            "- 추천/공유 유도\n"
            "- 100~150자 내외\n"
            "- 한국어로 작성"
        ),
    },
    "event": {
        "short": (
            "너는 이벤트 홍보 전문가야. "
            "{topic}에 대한 짧은 이벤트 안내 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 날짜/시간/장소 명시\n"
            "- 참여 방법 간략히\n"
            "- 70자 이내\n"
            "- 한국어로 작성"
        ),
        "emotional": (
            "너는 이벤트 스토리텔러야. "
            "{topic}에 대한 감성적인 이벤트 안내 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 이벤트의 의미/가치 강조\n"
            "- 설렘과 기대감 표현\n"
            "- 120~180자 내외\n"
            "- 한국어로 작성"
        ),
        "intense": (
            "너는 하이퍼 이벤트 프로모터야. "
            "{topic}에 대한 강렬한 이벤트 안내 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 한정 시간/인원 강조\n"
            "- 놓치면 후회할 요소 부각\n"
            "- 즉시 신청 유도\n"
            "- 100~150자 내외\n"
            "- 한국어로 작성"
        ),
    },
}


# ─── Public API ───────────────────────────────────────────────────────────


def get_random_content_type() -> str:
    return random.choice(CONTENT_TYPES)


def build_prompt(content_type: str, tone: str, topic: str | None = None, context: str | None = None) -> str:
    template = _TEMPLATES.get(content_type, {}).get(tone)
    if template is None:
        template = (
            "너는 텔레그램 마케팅 전문가야. "
            "주제에 맞는 마케팅 메시지를 작성해줘.\n\n"
            "규칙:\n"
            "- 한국어로 작성\n"
            "- 간결하게\n"
            "- 행동 유도 포함"
        )

    prompt = template.format(topic=topic or "일반적인 주제")

    if context:
        prompt += f"\n\n[추가 컨텍스트]\n{context}"

    return prompt


async def generate_content(
    content_type: str,
    tone: str,
    topic: str | None = None,
    context: str | None = None,
    tenant_id: str | None = None,
    style_profile_id: str | None = None,
    db: AsyncSession | None = None,
) -> tuple[str | None, int]:
    """Generate content via DeepSeek and record usage.

    Returns (generated_content, tokens_used) or (None, 0) on failure.
    """
    prompt = build_prompt(content_type, tone, topic, context)

    if style_profile_id and db is not None:
        try:
            from app.services.ai_style_service import get_style_prompt_for_generation
            style_prompt = await get_style_prompt_for_generation(style_profile_id, db)
            if style_prompt:
                prompt = f"{style_prompt}\n\n{prompt}"
        except Exception:
            pass

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "위 지시에 따라 메시지를 생성해줘."},
    ]

    reply, tokens = await call_deepseek(messages)
    if reply is None:
        return None, 0

    if tenant_id:
        try:
            await record_ai_usage(
                db=None,
                tenant_id=tenant_id,
                feature=FEATURE_CONTENT_STUDIO,
                tokens_used=tokens,
            )
        except Exception:
            pass

    return reply.strip(), tokens
