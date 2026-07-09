"""
Delivery Analytics service.

Provides tenant-safe, account-safe operational analytics derived from
persisted MessageLog records (Sprint 14 canonical delivery pipeline).

Ownership path: MessageLog.account_id → Account.tenant_id → Identity.tenant_id

All functions require an Identity for tenant authorization.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Integer, case, func, select

from app.api.deps import Identity
from app.database import async_session_maker
from app.models.account import Account
from app.models.message_log import MessageLog


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── Response types ──────────────────────────────────────────────────


@dataclass
class SummaryResult:
    total_attempted: int = 0
    successful: int = 0
    failed: int = 0
    success_rate: float = 0.0


@dataclass
class FailureBreakdownItem:
    status: str
    count: int


@dataclass
class AccountPerformanceItem:
    account_id: str
    attempted: int = 0
    successful: int = 0
    failed: int = 0
    success_rate: float = 0.0


@dataclass
class TimelineItem:
    period: str
    attempted: int = 0
    successful: int = 0
    failed: int = 0


@dataclass
class RecentActivityItem:
    id: str
    account_id: str
    recipient: str
    source: str
    status: str
    success: bool
    error_message: str | None = None
    telegram_message_id: int | None = None
    attempt_count: int = 1
    created_at: str | None = None


# ─── Authorization helpers ───────────────────────────────────────────


async def _resolve_authorized_account_ids(
    identity: Identity,
    account_id: str | None = None,
) -> list[str]:
    """Resolve the set of account_ids visible to this identity.

    Admin sees all. Tenant user sees own tenant's accounts.
    Optional account_id filters to a single account if authorized.
    Returns empty list if no accounts are authorized (fail-closed).
    """
    async with async_session_maker() as db:
        if identity.kind == "admin":
            query = select(Account.id)
            if account_id:
                query = query.where(Account.id == account_id)
            result = await db.execute(query)
            return [r[0] for r in result.all()]

        if identity.tenant_id is None:
            return []

        query = select(Account.id).where(Account.tenant_id == identity.tenant_id)
        if account_id:
            query = query.where(Account.id == account_id)
        result = await db.execute(query)
        return [r[0] for r in result.all()]


# ─── Analytics queries ────────────────────────────────────────────────


async def get_summary(
    identity: Identity,
    account_id: str | None = None,
    days: int = 30,
) -> SummaryResult:
    """Get delivery summary for authorized accounts."""
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return SummaryResult()

    since = utcnow_naive()

    async with async_session_maker() as db:
        result = await db.execute(
            select(
                func.count(MessageLog.id).label("total"),
                func.sum(case((MessageLog.success.is_(True), 1), else_=0)).label("successful"),
            ).where(
                MessageLog.account_id.in_(account_ids),
                MessageLog.created_at >= since,
            )
        )
        row = result.one()

    total = row.total or 0
    successful = row.successful or 0
    failed = total - successful
    rate = (successful / total * 100.0) if total > 0 else 0.0

    return SummaryResult(
        total_attempted=total,
        successful=successful,
        failed=failed,
        success_rate=round(rate, 1),
    )


async def get_failure_breakdown(
    identity: Identity,
    account_id: str | None = None,
    days: int = 30,
) -> list[FailureBreakdownItem]:
    """Get failure breakdown by DeliveryStatus (excluding SUCCESS)."""
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    since = utcnow_naive()

    async with async_session_maker() as db:
        result = await db.execute(
            select(
                MessageLog.status,
                func.count(MessageLog.id).label("count"),
            ).where(
                MessageLog.account_id.in_(account_ids),
                MessageLog.created_at >= since,
                MessageLog.success.is_(False),
            ).group_by(MessageLog.status)
        )
        return [FailureBreakdownItem(status=r[0], count=r[1]) for r in result.all()]


async def get_account_performance(
    identity: Identity,
    days: int = 30,
) -> list[AccountPerformanceItem]:
    """Get delivery performance per authorized account."""
    account_ids = await _resolve_authorized_account_ids(identity)
    if not account_ids:
        return []

    since = utcnow_naive()

    async with async_session_maker() as db:
        result = await db.execute(
            select(
                MessageLog.account_id,
                func.count(MessageLog.id).label("total"),
                func.sum(case((MessageLog.success.is_(True), 1), else_=0)).label("successful"),
            ).where(
                MessageLog.account_id.in_(account_ids),
                MessageLog.created_at >= since,
            ).group_by(MessageLog.account_id)
        )
        items = []
        for row in result.all():
            total = row.total or 0
            successful = row.successful or 0
            failed = total - successful
            rate = (successful / total * 100.0) if total > 0 else 0.0
            items.append(AccountPerformanceItem(
                account_id=row.account_id,
                attempted=total,
                successful=successful,
                failed=failed,
                success_rate=round(rate, 1),
            ))
        return items


async def get_timeline(
    identity: Identity,
    account_id: str | None = None,
    days: int = 30,
    interval: str = "day",
) -> list[TimelineItem]:
    """Get delivery timeline grouped by hour or day."""
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    since = utcnow_naive()

    async with async_session_maker() as db:
        if interval == "hour":
            date_expr = func.strftime("%Y-%m-%dT%H:00", MessageLog.created_at)
        else:
            date_expr = func.strftime("%Y-%m-%d", MessageLog.created_at)

        result = await db.execute(
            select(
                date_expr.label("period"),
                func.count(MessageLog.id).label("total"),
                func.sum(case((MessageLog.success.is_(True), 1), else_=0)).label("successful"),
            ).where(
                MessageLog.account_id.in_(account_ids),
                MessageLog.created_at >= since,
            ).group_by(date_expr).order_by(date_expr)
        )
        return [
            TimelineItem(
                period=row.period,
                attempted=row.total or 0,
                successful=row.successful or 0,
                failed=(row.total or 0) - (row.successful or 0),
            )
            for row in result.all()
        ]


async def get_recent_activity(
    identity: Identity,
    account_id: str | None = None,
    limit: int = 50,
) -> list[RecentActivityItem]:
    """Get most recent delivery activity for authorized accounts."""
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    limit = min(limit, 200)

    async with async_session_maker() as db:
        result = await db.execute(
            select(MessageLog).where(
                MessageLog.account_id.in_(account_ids),
            ).order_by(MessageLog.created_at.desc()).limit(limit)
        )
        rows = list(result.scalars().all())
        return [
            RecentActivityItem(
                id=row.id,
                account_id=row.account_id,
                recipient=row.recipient,
                source=row.source,
                status=row.status,
                success=row.success,
                error_message=row.error_message,
                telegram_message_id=row.telegram_message_id,
                attempt_count=row.attempt_count,
                created_at=row.created_at.isoformat() if row.created_at else None,
            )
            for row in rows
        ]