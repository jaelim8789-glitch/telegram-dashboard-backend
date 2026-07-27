"""Account Health API  tenant-isolated health monitoring with operational summary."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity
from app.database import async_session_maker, get_db
from app.models.account import Account
from app.models.message_log import MessageLog
from app.services.account_health import get_account_health, HealthSummary, AccountHealthItem

router = APIRouter(prefix="/api/account-health", tags=["account-health"])


@router.get("")
async def api_account_health(
    account_id: str | None = None,
    identity: Identity = Depends(get_current_identity),
):
    """Get detailed account health status for all authorized accounts.

    Derives health from Account model fields (status, session_data) and
    recent MessageLog delivery outcomes. Tenant-isolated.
    """
    result = await get_account_health(identity, account_id=account_id)
    return result


@router.get("/summary")
async def get_account_health_summary(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Return aggregate account health statistics."""
    from sqlalchemy import case as case_

    tenant_condition = []
    if identity.kind != "admin" and identity.tenant_id:
        tenant_condition.append(Account.tenant_id == identity.tenant_id)

    query = select(
        func.count(Account.id).label("total"),
        func.sum(case_((Account.status == "active", 1), else_=0)).label("active"),
        func.sum(case_((Account.status == "inactive", 1), else_=0)).label("inactive"),
        func.sum(case_((Account.status == "banned", 1), else_=0)).label("banned"),
        func.sum(case_((Account.last_error.isnot(None), 1), else_=0)).label("has_errors"),
    ).where(*tenant_condition)

    result = await db.execute(query)
    row = result.one()
    return {
        "total": row.total or 0,
        "active": row.active or 0,
        "inactive": row.inactive or 0,
        "banned": row.banned or 0,
        "has_errors": row.has_errors or 0,
    }


@router.get("/trend")
async def api_health_trend(
    days: int = Query(default=14, le=90, ge=1),
    identity: Identity = Depends(get_current_identity),
):
    """Get health trend data over time (daily success/failure snapshots)."""
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    async with async_session_maker() as db:
        if identity.kind == "admin":
            query = select(Account)
        elif identity.tenant_id:
            query = select(Account).where(Account.tenant_id == identity.tenant_id)
        else:
            return {"trend": []}

        result = await db.execute(query)
        accounts = list(result.scalars().all())
        account_ids = [a.id for a in accounts]

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

        trend = []
        for i in range(days):
            d = (datetime.now(timezone.utc) - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
            if d in daily_data:
                dd = daily_data[d]
                rate = round((dd["successful"] / dd["total"] * 100), 1) if dd["total"] > 0 else 0
                trend.append({
                    "date": d, "total": dd["total"], "successful": dd["successful"],
                    "failed": dd["failed"], "success_rate": rate,
                    "active_accounts": len(dd["accounts"]),
                })
            else:
                trend.append({"date": d, "total": 0, "successful": 0, "failed": 0, "success_rate": 0, "active_accounts": 0})

    return {"trend": trend}
