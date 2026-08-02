"""Trust API — trust profiles, reviews, and score calculation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.api.deps import get_current_identity, Identity
from app.models.trust import TrustProfile, TransactionReview

router = APIRouter(tags=["trust"])


# ── Schemas ──────────────────────────────────────────────────────────


class ReviewCreateRequest(BaseModel):
    escrow_id: str
    target_id: str
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = None
    tags: list[str] | None = None


# ── Trust Profile ────────────────────────────────────────────────────


@router.get("/api/trust/{telegram_user_id}")
async def get_trust_profile(
    telegram_user_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    result = await db.execute(
        select(TrustProfile).where(
            TrustProfile.tenant_id == identity.tenant_id,
            TrustProfile.telegram_user_id == telegram_user_id,
        )
    )
    profile = result.scalar_one_or_none()
    if not profile:
        return {
            "telegram_user_id": telegram_user_id,
            "trust_score": 0,
            "total_transactions": 0,
            "successful_transactions": 0,
            "avg_rating": 0,
            "badges": [],
            "is_new": True,
        }
    return _profile_to_dict(profile)


@router.get("/api/trust")
async def list_trust_profiles(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    result = await db.execute(
        select(TrustProfile)
        .where(TrustProfile.tenant_id == identity.tenant_id)
        .order_by(TrustProfile.trust_score.desc())
        .limit(100)
    )
    return [_profile_to_dict(p) for p in result.scalars().all()]


# ── Reviews ──────────────────────────────────────────────────────────


@router.post("/api/trust/reviews")
async def create_review(
    payload: ReviewCreateRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    review = TransactionReview(
        escrow_id=payload.escrow_id,
        tenant_id=identity.tenant_id,
        reviewer_id=identity.tenant_id,
        target_id=payload.target_id,
        rating=payload.rating,
        comment=payload.comment,
        tags=payload.tags,
    )
    db.add(review)
    await db.commit()

    await _update_trust_score(db, identity.tenant_id, payload.target_id)

    await db.refresh(review)
    return _review_to_dict(review)


@router.get("/api/trust/reviews/{target_id}")
async def get_reviews(
    target_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    result = await db.execute(
        select(TransactionReview)
        .where(
            TransactionReview.tenant_id == identity.tenant_id,
            TransactionReview.target_id == target_id,
        )
        .order_by(TransactionReview.created_at.desc())
        .limit(50)
    )
    return [_review_to_dict(r) for r in result.scalars().all()]


# ── Score Calculation ────────────────────────────────────────────────


async def _update_trust_score(db: AsyncSession, tenant_id: str, telegram_user_id: str):
    """Recalculate trust score from all reviews for this user."""
    result = await db.execute(
        select(
            func.count(TransactionReview.id).label("total"),
            func.avg(TransactionReview.rating).label("avg_rating"),
        )
        .where(
            TransactionReview.tenant_id == tenant_id,
            TransactionReview.target_id == telegram_user_id,
        )
    )
    row = result.one()
    total = row.total or 0
    avg_rating = float(row.avg_rating or 0)

    result2 = await db.execute(
        select(func.count(TransactionReview.id))
        .where(
            TransactionReview.tenant_id == tenant_id,
            TransactionReview.target_id == telegram_user_id,
            TransactionReview.rating >= 4,
        )
    )
    successful = result2.scalar() or 0

    result3 = await db.execute(
        select(func.sum(TransactionReview.rating))
        .where(
            TransactionReview.tenant_id == tenant_id,
            TransactionReview.target_id == telegram_user_id,
        )
    )
    total_score = float(result3.scalar() or 0)

    trust_score = _calculate_score(total, successful, avg_rating, total_score)

    profile_result = await db.execute(
        select(TrustProfile).where(
            TrustProfile.tenant_id == tenant_id,
            TrustProfile.telegram_user_id == telegram_user_id,
        )
    )
    profile = profile_result.scalar_one_or_none()

    if not profile:
        profile = TrustProfile(
            tenant_id=tenant_id,
            telegram_user_id=telegram_user_id,
        )
        db.add(profile)

    profile.total_transactions = total
    profile.successful_transactions = successful
    profile.avg_rating = avg_rating
    profile.trust_score = trust_score
    profile.badges = _compute_badges(total, successful, avg_rating)

    await db.commit()


def _calculate_score(total: int, successful: int, avg_rating: float, total_score: float) -> float:
    if total == 0:
        return 0.0

    success_rate = successful / total if total > 0 else 0
    rating_norm = avg_rating / 5.0 if avg_rating > 0 else 0
    volume_norm = min(total / 50.0, 1.0)

    score = (
        success_rate * 40
        + rating_norm * 30
        + volume_norm * 20
        + min(total_score / (total * 5), 1.0) * 10
    )
    return round(min(score, 100.0), 1)


def _compute_badges(total: int, successful: int, avg_rating: float) -> list[str]:
    badges = []
    if total >= 50:
        badges.append("power_trader")
    if total >= 10:
        badges.append("active_trader")
    if total >= 5:
        badges.append("verified")
    if avg_rating >= 4.5 and total >= 10:
        badges.append("top_rated")
    if avg_rating >= 4.0 and total >= 5:
        badges.append("trusted")
    if total > 0 and successful == total:
        badges.append("flawless")
    return badges


# ── Helpers ──────────────────────────────────────────────────────────


def _profile_to_dict(p: TrustProfile) -> dict:
    return {
        "id": p.id,
        "telegram_user_id": p.telegram_user_id,
        "display_name": p.display_name,
        "trust_score": p.trust_score,
        "total_transactions": p.total_transactions,
        "successful_transactions": p.successful_transactions,
        "disputed_transactions": p.disputed_transactions,
        "avg_rating": round(p.avg_rating, 1),
        "badges": p.badges or [],
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _review_to_dict(r: TransactionReview) -> dict:
    return {
        "id": r.id,
        "escrow_id": r.escrow_id,
        "reviewer_id": r.reviewer_id,
        "target_id": r.target_id,
        "rating": r.rating,
        "comment": r.comment,
        "tags": r.tags or [],
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
