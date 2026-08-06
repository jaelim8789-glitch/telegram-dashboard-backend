"""AI Group Intelligence  analyzes Telegram groups an account belongs to.

Uses Ollama to classify groups by topic, engagement level, size, and
recommend the best targets for a broadcast goal.

All endpoints reuse ``_call_ollama`` from ``app.services.ai_chat_service``
so the same Ollama configuration, provider, and quota model applies.
Every endpoint is gated by the standard authentication dependency.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Identity, get_current_identity, require_account_tenant_access
from app.crud import account as account_crud
from app.database import get_db
from app.services.ai_chat_service import _call_ollama
from app.services.telegram_actions import AccountNotAuthenticatedError, list_groups

router = APIRouter(prefix="/api/ai/groups", tags=["ai-group-intel"])

logger = __import__("app.core.logging", fromlist=["get_logger"]).get_logger(__name__)


#  Schemas 


class GroupClassification(BaseModel):
    chat_id: str
    title: str
    type: str  # "group", "megagroup", "channel"
    participants_count: int = 0
    topic: str = ""  # AI-classified topic
    engagement_level: str = ""  # "high", "medium", "low"
    size_category: str = ""  # "small", "medium", "large", "xlarge"
    language: str = ""
    description: str = ""


class AnalyzeGroupsResponse(BaseModel):
    groups: list[GroupClassification]
    summary: str = ""
    total_analyzed: int = 0


class BestTargetRequest(BaseModel):
    broadcast_purpose: str = Field(
        ..., min_length=1, max_length=1000,
        description="   : '  ', 'VIP     '",
    )
    max_recommendations: int = Field(default=5, ge=1, le=20)


class TargetRecommendation(BaseModel):
    chat_id: str
    title: str
    reason: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    estimated_reach: int = 0


class BestTargetResponse(BaseModel):
    purpose: str
    recommendations: list[TargetRecommendation]
    reasoning_summary: str = ""


class GroupAnalyticsResponse(BaseModel):
    total_groups: int = 0
    total_channels: int = 0
    total_participants: int = 0
    by_topic: dict[str, int] = {}
    by_engagement: dict[str, int] = {}
    by_size: dict[str, int] = {}
    top_groups: list[dict] = []
    top_channels: list[dict] = []


#  Internal helpers 


async def _fetch_groups(account_id: str, db: AsyncSession) -> list[dict]:
    """Fetch all groups/channels for an account with error handling."""
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    try:
        return await list_groups(account)
    except AccountNotAuthenticatedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


def _classify_size(participants: int) -> str:
    if participants <= 50:
        return "small"
    elif participants <= 500:
        return "medium"
    elif participants <= 5000:
        return "large"
    else:
        return "xlarge"


#  Endpoints 


@router.get("/{account_id}/analyze", response_model=AnalyzeGroupsResponse)
async def analyze_groups(
    account_id: str = Path(..., description="Telegram account ID"),
    min_members: int = Query(default=0, ge=0, description="   "),
    max_groups: int = Query(default=50, ge=1, le=200, description="   "),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
) -> AnalyzeGroupsResponse:
    """Analyze and classify all groups/channels an account belongs to.

    Uses Ollama to classify each group's topic, engagement level,
    and language based on its title and metadata.
    """
    await require_account_tenant_access(account_id, db, identity)
    all_groups = await _fetch_groups(account_id, db)

    # Filter by minimum members
    filtered = [g for g in all_groups if (g.get("participants_count") or 0) >= min_members]
    # Sort by participants descending, take top N
    filtered.sort(key=lambda g: g.get("participants_count") or 0, reverse=True)
    groups_to_analyze = filtered[:max_groups]

    if not groups_to_analyze:
        return AnalyzeGroupsResponse(groups=[], summary="  .", total_analyzed=0)

    # Prepare group data for AI
    group_lines = []
    for g in groups_to_analyze:
        group_lines.append(
            f"- ID: {g['chat_id']} | : {g.get('title', '')} | : {g.get('type', '')} | "
            f": {g.get('participants_count', 0)}"
        )

    system_prompt = (
        " TeleMon AI   .  /   "
        " (topic), (engagement_level), (language) .\n\n"
        "  JSON   (  ):\n"
        "{\n"
        '  "classifications": [\n'
        "    {\n"
        '      "chat_id": "ID",\n'
        '      "topic": "  (, 2-4)",\n'
        '      "engagement_level": "high|medium|low",\n'
        '      "language": "ko|en|ja|zh|etc"\n'
        "    }\n"
        "  ],\n"
        '  "summary": "   (, 2-3)"\n'
        "}\n\n"
        " :\n"
        "- topic:      (: , , , , , , ,  )\n"
        "- engagement_level: () medium/high, () low/medium  \n"
        "- language:    \n"
        "-  summary "
    )
    user_prompt = f"[ /  ({len(groups_to_analyze)})]\n" + "\n".join(group_lines)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    reply = await _call_ollama(messages)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI   .    .",
        )

    try:
        parsed = json.loads(reply.strip())
        classifications = parsed.get("classifications", [])
        summary = parsed.get("summary", "")
    except (json.JSONDecodeError, TypeError, ValueError):
        classifications = []
        summary = "     ."

    # Merge classifications back into group data
    class_map = {c["chat_id"]: c for c in classifications if "chat_id" in c}
    result_groups = []
    for g in groups_to_analyze:
        cid = g["chat_id"]
        cls = class_map.get(cid, {})
        result_groups.append(
            GroupClassification(
                chat_id=cid,
                title=g.get("title", ""),
                type=g.get("type", ""),
                participants_count=g.get("participants_count") or 0,
                topic=cls.get("topic", ""),
                engagement_level=cls.get("engagement_level", "unknown"),
                size_category=_classify_size(g.get("participants_count") or 0),
                language=cls.get("language", ""),
                description=g.get("description", ""),
            )
        )

    return AnalyzeGroupsResponse(
        groups=result_groups,
        summary=summary,
        total_analyzed=len(result_groups),
    )


@router.post("/{account_id}/best-targets", response_model=BestTargetResponse)
async def recommend_best_targets(
    account_id: str,
    payload: BestTargetRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
) -> BestTargetResponse:
    """Recommend the best groups/channels to target for a specific broadcast purpose.

    AI analyzes all groups and selects the most relevant ones based on
    the broadcast goal, considering topic fit, audience size, and engagement.
    """
    await require_account_tenant_access(account_id, db, identity)
    all_groups = await _fetch_groups(account_id, db)

    # Sort by participants, take top 100 for analysis
    sorted_groups = sorted(all_groups, key=lambda g: g.get("participants_count") or 0, reverse=True)
    candidates = sorted_groups[:100]

    if not candidates:
        return BestTargetResponse(
            purpose=payload.broadcast_purpose,
            recommendations=[],
            reasoning_summary="  / .",
        )

    group_lines = []
    for g in candidates:
        group_lines.append(
            f"- ID: {g['chat_id']} | : {g.get('title', '')} | : {g.get('type', '')} | "
            f": {g.get('participants_count', 0)}"
        )

    system_prompt = (
        " TeleMon AI   .  /  "
        "      .\n\n"
        f"[ ]\n{payload.broadcast_purpose}\n\n"
        f"  : {payload.max_recommendations}\n\n"
        "  JSON   (  ):\n"
        "{\n"
        '  "recommendations": [\n'
        "    {\n"
        '      "chat_id": "ID",\n'
        '      "reason": "  ()",\n'
        '      "confidence": 0.0~1.0,\n'
        '      "estimated_reach": \n'
        "    }\n"
        "  ],\n"
        '  "reasoning_summary": "    (, 2-3)"\n'
        "}\n\n"
        " :\n"
        "-     \n"
        "-   ( )\n"
        "-   (=, =)\n"
        "- confidence    (   )"
    )
    user_prompt = f"[ /  ({len(candidates)})]\n" + "\n".join(group_lines)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    reply = await _call_ollama(messages)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI   .    .",
        )

    try:
        parsed = json.loads(reply.strip())
        recs_data = parsed.get("recommendations", [])
        reasoning_summary = parsed.get("reasoning_summary", "")
    except (json.JSONDecodeError, TypeError, ValueError):
        recs_data = []
        reasoning_summary = "     ."

    # Build a lookup for candidate titles
    title_map = {g["chat_id"]: g.get("title", "") for g in candidates}
    reach_map = {g["chat_id"]: g.get("participants_count") or 0 for g in candidates}

    recommendations = []
    for rec in recs_data[: payload.max_recommendations]:
        cid = rec.get("chat_id", "")
        if cid in title_map:
            recommendations.append(
                TargetRecommendation(
                    chat_id=cid,
                    title=title_map.get(cid, ""),
                    reason=rec.get("reason", ""),
                    confidence=min(max(float(rec.get("confidence", 0.5)), 0.0), 1.0),
                    estimated_reach=reach_map.get(cid, 0),
                )
            )

    return BestTargetResponse(
        purpose=payload.broadcast_purpose,
        recommendations=recommendations,
        reasoning_summary=reasoning_summary,
    )


@router.get("/{account_id}/analytics", response_model=GroupAnalyticsResponse)
async def group_analytics(
    account_id: str = Path(..., description="Telegram account ID"),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
) -> GroupAnalyticsResponse:
    """Get aggregate group analytics for an account  counts by type, size
    distribution, and top groups/channels. No AI call needed; this is a
    lightweight stats endpoint.
    """
    await require_account_tenant_access(account_id, db, identity)
    all_groups = await _fetch_groups(account_id, db)

    groups_list = [g for g in all_groups if g.get("type") in ("group", "megagroup")]
    channels_list = [g for g in all_groups if g.get("type") == "channel"]

    total_participants = sum(g.get("participants_count") or 0 for g in all_groups)

    # Size distribution
    size_dist = {"small": 0, "medium": 0, "large": 0, "xlarge": 0}
    for g in all_groups:
        cat = _classify_size(g.get("participants_count") or 0)
        size_dist[cat] = size_dist.get(cat, 0) + 1

    # Top groups and channels (by participants)
    groups_sorted = sorted(groups_list, key=lambda g: g.get("participants_count") or 0, reverse=True)
    channels_sorted = sorted(channels_list, key=lambda g: g.get("participants_count") or 0, reverse=True)

    def _to_summary(g: dict) -> dict:
        return {
            "chat_id": g.get("chat_id", ""),
            "title": g.get("title", ""),
            "participants": g.get("participants_count") or 0,
            "type": g.get("type", ""),
        }

    return GroupAnalyticsResponse(
        total_groups=len(groups_list),
        total_channels=len(channels_list),
        total_participants=total_participants,
        by_size=size_dist,
        top_groups=[_to_summary(g) for g in groups_sorted[:10]],
        top_channels=[_to_summary(g) for g in channels_sorted[:10]],
    )