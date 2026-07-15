"""Account Health Monitoring — derived from existing account and delivery data.

Health states are determined by combining:
1. Account model fields (status, session_data, last_activity, last_error*)
2. Recent MessageLog delivery outcomes (last 100 attempts per account)

No fake online status, no WebSocket, no external pings.
All data is already persisted by the canonical delivery pipeline.

Health states (mutually exclusive, highest-priority-first):
- banned: Account.status == "banned" or most recent delivery was banned
- unauthorized: No valid session or most recent delivery was session_expired
- rate_limited: Most recent delivery was flood_wait
- error: Most recent delivery was a non-recoverable error
- healthy: Has session, recent successful delivery, no recent errors
- unknown: No delivery history, has session configured
- not_configured: No session_data
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, case

from app.api.deps import Identity
from app.database import async_session_maker
from app.models.account import Account
from app.models.message_log import MessageLog


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class AccountHealthItem:
    account_id: str
    phone: str
    name: str | None = None
    status: str = "unknown"
    has_session: bool = False
    last_activity: str | None = None
    last_error: str | None = None
    last_error_status: str | None = None
    last_error_at: str | None = None
    last_success_at: str | None = None
    health_checked_at: str | None = None
    recent_success_count: int = 0
    recent_failure_count: int = 0
    total_delivery_attempts: int = 0


@dataclass
class HealthSummary:
    total: int
    healthy: int
    unhealthy: int
    not_configured: int
    banned: int
    rate_limited: int
    unauthorized: int
    error_count: int
    unknown: int
    has_session: int
    has_errors: int
    total_today_sent: int
    total_groups: int


async def get_account_health(
    identity: Identity,
    account_id: str | None = None,
) -> list[AccountHealthItem]:
    """Get health status for all authorized accounts."""
    async with async_session_maker() as db:
        if identity.kind == "admin":
            query = select(Account)
            if account_id:
                query = query.where(Account.id == account_id)
        elif identity.tenant_id:
            query = select(Account).where(Account.tenant_id == identity.tenant_id)
            if account_id:
                query = query.where(Account.id == account_id)
        else:
            return []

        query = query.order_by(Account.created_at.desc())
        result = await db.execute(query)
        accounts = list(result.scalars().all())

    if not accounts:
        return []

    account_ids = [a.id for a in accounts]
    since = utcnow_naive() - timedelta(days=7)

    async with async_session_maker() as db:
        latest_q = select(
            MessageLog
        ).distinct(
            MessageLog.account_id
        ).where(
            MessageLog.account_id.in_(account_ids),
            MessageLog.created_at >= since,
        ).order_by(
            MessageLog.account_id,
            MessageLog.created_at.desc(),
        )
        latest_result = await db.execute(latest_q)
        latest_rows = {log.account_id: log for log in latest_result.scalars().all()}

        counts_q = select(
            MessageLog.account_id,
            func.count(MessageLog.id).label("total"),
            func.sum(case((MessageLog.success.is_(True), 1), else_=0)).label("successful"),
        ).where(
            MessageLog.account_id.in_(account_ids),
            MessageLog.created_at >= since,
        ).group_by(MessageLog.account_id)
        counts_result = await db.execute(counts_q)
        counts = {}
        for row in counts_result.all():
            counts[row.account_id] = {
                "total": row.total or 0,
                "successful": row.successful or 0,
            }

    items = []
    for account in accounts:
        latest = latest_rows.get(account.id)
        acct_counts = counts.get(account.id, {"total": 0, "successful": 0})

        has_session = account.session_data is not None and len(account.session_data) > 0
        recent_success = acct_counts["successful"]
        recent_failures = acct_counts["total"] - acct_counts["successful"]

        if account.status == "banned":
            status_val = "banned"
        elif not has_session:
            status_val = "not_configured"
        elif latest and not latest.success:
            if latest.status == "session_expired":
                status_val = "unauthorized"
            elif latest.status == "banned":
                status_val = "banned"
            elif latest.status == "flood_wait":
                status_val = "rate_limited"
            elif latest.status in ("network_error", "internal_error"):
                status_val = "error"
            else:
                status_val = "error"
        elif latest and latest.success:
            status_val = "healthy"
        elif has_session:
            status_val = "unknown"
        else:
            status_val = "not_configured"

        last_error = latest.error_message if latest and not latest.success else None
        last_error_status = latest.status if latest and not latest.success else None

        items.append(AccountHealthItem(
            account_id=account.id,
            phone=account.phone,
            name=account.name,
            status=status_val,
            has_session=has_session,
            last_activity=latest.created_at.isoformat() if latest and latest.created_at else (account.last_activity.isoformat() if account.last_activity else None),
            last_error=last_error,
            last_error_status=last_error_status,
            last_error_at=account.last_error_at.isoformat() if account.last_error_at else None,
            last_success_at=account.last_success_at.isoformat() if account.last_success_at else None,
            health_checked_at=account.health_checked_at.isoformat() if account.health_checked_at else None,
            recent_success_count=recent_success,
            recent_failure_count=recent_failures,
            total_delivery_attempts=acct_counts["total"],
        ))

    return items


async def get_health_summary(
    identity: Identity,
) -> HealthSummary:
    """Get aggregated health summary for the tenant."""
    items = await get_account_health(identity)

    total = len(items)
    healthy = sum(1 for i in items if i.status == "healthy")
    not_configured = sum(1 for i in items if i.status == "not_configured")
    banned = sum(1 for i in items if i.status == "banned")
    rate_limited = sum(1 for i in items if i.status == "rate_limited")
    unauthorized = sum(1 for i in items if i.status == "unauthorized")
    error_count = sum(1 for i in items if i.status == "error")
    unknown = sum(1 for i in items if i.status == "unknown")
    has_session = sum(1 for i in items if i.has_session)
    has_errors = sum(1 for i in items if i.last_error is not None)

    async with async_session_maker() as db:
        from app.crud.account import get_account_summary
        summary = await get_account_summary(db, identity.tenant_id if identity.kind != "admin" else None)

    return HealthSummary(
        total=total,
        healthy=healthy,
        unhealthy=total - healthy,
        not_configured=not_configured,
        banned=banned,
        rate_limited=rate_limited,
        unauthorized=unauthorized,
        error_count=error_count,
        unknown=unknown,
        has_session=has_session,
        has_errors=has_errors,
        total_today_sent=summary["total_today_sent"],
        total_groups=summary["total_groups"],
    )
