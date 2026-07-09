"""Account Health Monitoring — derived from existing account and delivery data.

Health states are determined by combining:
1. Account model fields (status, session_data, last_activity)
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
    status: str = "unknown"  # healthy, unauthorized, banned, rate_limited, error, unknown, not_configured
    has_session: bool = False
    last_activity: str | None = None
    last_error: str | None = None
    last_error_status: str | None = None
    recent_success_count: int = 0
    recent_failure_count: int = 0
    total_delivery_attempts: int = 0


async def get_account_health(
    identity: Identity,
    account_id: str | None = None,
) -> list[AccountHealthItem]:
    """Get health status for all authorized accounts.

    Derives health from Account model fields and recent MessageLog data.
    Tenant-isolated: users only see their own tenant's accounts.
    """
    # Resolve authorized account IDs
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

    # Get recent delivery stats per account (last 7 days)
    since = utcnow_naive() - timedelta(days=7)

    async with async_session_maker() as db:
        # Most recent delivery per account
        latest_rows = {}
        for aid in account_ids:
            latest_q = select(MessageLog).where(
                MessageLog.account_id == aid,
                MessageLog.created_at >= since,
            ).order_by(MessageLog.created_at.desc()).limit(1)
            latest_result = await db.execute(latest_q)
            latest_list = list(latest_result.scalars().all())
            if latest_list:
                latest_rows[aid] = latest_list[0]

        # Counts per account
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

    # Build health items
    items = []
    for account in accounts:
        latest = latest_rows.get(account.id)
        acct_counts = counts.get(account.id, {"total": 0, "successful": 0})

        has_session = account.session_data is not None and len(account.session_data) > 0
        recent_success = acct_counts["successful"]
        recent_failures = acct_counts["total"] - acct_counts["successful"]

        # Determine health status
        if account.status == "banned":
            status = "banned"
        elif not has_session:
            status = "not_configured"
        elif latest and not latest.success:
            # Most recent delivery failed — classify the failure
            if latest.status == "session_expired":
                status = "unauthorized"
            elif latest.status == "banned":
                status = "banned"
            elif latest.status == "flood_wait":
                status = "rate_limited"
            elif latest.status in ("network_error", "internal_error"):
                status = "error"
            else:
                status = "error"
        elif latest and latest.success:
            status = "healthy"
        elif has_session:
            # Has session but no recent delivery history
            status = "unknown"
        else:
            status = "not_configured"

        last_error = latest.error_message if latest and not latest.success else None
        last_error_status = latest.status if latest and not latest.success else None

        items.append(AccountHealthItem(
            account_id=account.id,
            phone=account.phone,
            name=account.name,
            status=status,
            has_session=has_session,
            last_activity=latest.created_at.isoformat() if latest and latest.created_at else (account.last_activity.isoformat() if account.last_activity else None),
            last_error=last_error,
            last_error_status=last_error_status,
            recent_success_count=recent_success,
            recent_failure_count=recent_failures,
            total_delivery_attempts=acct_counts["total"],
        ))

    return items