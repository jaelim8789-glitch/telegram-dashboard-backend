"""Health trend history — aggregated daily snapshots for visualization."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_identity, Identity
from app.database import async_session_maker
from app.models.account import Account
from app.models.message_log import MessageLog
from sqlalchemy import func, select, case
from app.services.account_health import get_account_health

router = APIRouter(prefix="/api/account-health", tags=["account-health"])


@router.get("/summary")
async def api_health_summary(
    identity: Identity = Depends(get_current_identity),
):
    """Get aggregated health summary with counts per state."""
    items = await get_account_health(identity)
    total = len(items)
    counts = {}
    for item in items:
        s = item.status
        counts[s] = counts.get(s, 0) + 1
    
    total_success = sum(i.recent_success_count for i in items)
    total_failure = sum(i.recent_failure_count for i in items)
    total_attempts = total_success + total_failure
    overall_rate = (total_success / total_attempts * 100) if total_attempts > 0 else 0

    # Compute health scores (0-100)
    health_scores = []
    for item in items:
        score = 100
        # Deduct for issues
        if item.status == "banned":
            score -= 80
        elif item.status == "unauthorized":
            score -= 60
        elif item.status == "not_configured":
            score -= 70
        elif item.status == "rate_limited":
            score -= 30
        elif item.status == "error":
            score -= 40
        # Failure rate penalty
        total_i = item.recent_success_count + item.recent_failure_count
        if total_i > 0:
            fail_rate = item.recent_failure_count / total_i
            score -= fail_rate * 50
        health_scores.append({
            "account_id": item.account_id,
            "score": max(0, score),
        })

    avg_score = sum(h["score"] for h in health_scores) / len(health_scores) if health_scores else 0

    return {
        "total": total,
        "counts": counts,
        "healthy_count": counts.get("healthy", 0),
        "unhealthy_count": total - counts.get("healthy", 0),
        "overall_success_rate": round(overall_rate, 1),
        "total_success": total_success,
        "total_failure": total_failure,
        "average_health_score": round(avg_score, 0),
        "health_scores": health_scores,
    }


@router.get("/trend")
async def api_health_trend(
    days: int = Query(default=14, le=90, ge=1),
    identity: Identity = Depends(get_current_identity),
):
    """Get health trend data over time (daily snapshots)."""
    from datetime import date as date_type
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    
    async with async_session_maker() as db:
        # Get accounts for this identity
        if identity.kind == "admin":
            query = select(Account)
        elif identity.tenant_id:
            query = select(Account).where(Account.tenant_id == identity.tenant_id)
        else:
            return {"trend": []}

        result = await db.execute(query)
        accounts = list(result.scalars().all())
        account_ids = [a.id for a in accounts]

    # Build daily trend from MessageLog
    async with async_session_maker() as db:
        trend_q = select(
            func.date(MessageLog.created_at).label("day"),
            MessageLog.account_id,
            func.count(MessageLog.id).label("total"),
            func.sum(case((MessageLog.success.is_(True), 1), else_=0)).label("successful"),
        ).where(
            MessageLog.account_id.in_(account_ids),
            MessageLog.created_at >= since,
        ).group_by(
            func.date(MessageLog.created_at),
            MessageLog.account_id,
        ).order_by(func.date(MessageLog.created_at))

        trend_result = await db.execute(trend_q)
        daily_data = {}
        for row in trend_result.all():
            day_str = str(row.day)
            if day_str not in daily_data:
                daily_data[day_str] = {"date": day_str, "total": 0, "successful": 0, "failed": 0, "accounts": set()}
            daily_data[day_str]["total"] += row.total
            daily_data[day_str]["successful"] += row.successful
            daily_data[day_str]["failed"] += row.total - row.successful
            daily_data[day_str]["accounts"].add(row.account_id)

        # Fill missing days
        trend = []
        for i in range(days):
            d = (datetime.now(timezone.utc) - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
            if d in daily_data:
                dd = daily_data[d]
                rate = round((dd["successful"] / dd["total"] * 100), 1) if dd["total"] > 0 else 0
                trend.append({
                    "date": d,
                    "total": dd["total"],
                    "successful": dd["successful"],
                    "failed": dd["failed"],
                    "success_rate": rate,
                    "active_accounts": len(dd["accounts"]),
                })
            else:
                trend.append({"date": d, "total": 0, "successful": 0, "failed": 0, "success_rate": 0, "active_accounts": 0})

    return {"trend": trend}