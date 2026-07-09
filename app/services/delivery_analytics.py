"""
Delivery Analytics service.

Provides tenant-safe, account-safe operational analytics derived from
persisted MessageLog records (Sprint 14 canonical delivery pipeline).

Ownership path: MessageLog.account_id → Account.tenant_id → Identity.tenant_id

All functions require an Identity for tenant authorization.

SEMANTICS — ATTEMPT-LEVEL ANALYTICS
------------------------------------
Each MessageLog row represents one delivery attempt to one recipient.
Retries create additional rows for the same (source, source_id, recipient).
The row with success=True is the authoritative final state for that recipient.

All counts in this module are attempt-level unless explicitly documented
as logical-delivery-level (e.g., broadcast analytics which deduplicates
by recipient within a broadcast_id).

Retries are NOT excluded from aggregate counts. A recipient that failed
twice and succeeded on the third attempt contributes 3 attempts (2 failed,
1 successful) to all aggregate metrics.
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


@dataclass
class SourceAnalyticsItem:
    source: str
    total: int = 0
    successful: int = 0
    failed: int = 0
    success_rate: float = 0.0


@dataclass
class BroadcastAnalyticsItem:
    broadcast_id: str
    total_recipients: int = 0
    successful: int = 0
    failed: int = 0
    success_rate: float = 0.0
    first_activity: str | None = None
    latest_activity: str | None = None


@dataclass
class FailureIntelligenceItem:
    status: str
    count: int
    percentage: float = 0.0
    affected_accounts: int = 0
    latest_occurrence: str | None = None


@dataclass
class OverviewResult:
    summary: SummaryResult | None = None
    by_source: list[SourceAnalyticsItem] | None = None
    top_accounts: list[AccountPerformanceItem] | None = None
    failure_breakdown: list[FailureIntelligenceItem] | None = None
    timeline: list[TimelineItem] | None = None


# ─── Filter helpers ──────────────────────────────────────────────────


def _apply_filters(
    query,
    account_ids: list[str],
    source: str | None = None,
    status: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
):
    """Apply common optional filters to a MessageLog query.

    All filters preserve tenant isolation via account_ids.
    Returns the modified query.
    """
    query = query.where(MessageLog.account_id.in_(account_ids))

    if source is not None:
        query = query.where(MessageLog.source == source)
    if status is not None:
        query = query.where(MessageLog.status == status)
    if start_time is not None:
        query = query.where(MessageLog.created_at >= start_time)
    if end_time is not None:
        query = query.where(MessageLog.created_at <= end_time)

    return query


def _parse_datetime_safe(value: str | None) -> datetime | None:
    """Parse an ISO datetime string safely. Returns None if invalid."""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


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
    source: str | None = None,
    status: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> SummaryResult:
    """Get delivery summary for authorized accounts with optional filters."""
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return SummaryResult()

    since = utcnow_naive()
    start_dt = _parse_datetime_safe(start_time) if start_time else since
    end_dt = _parse_datetime_safe(end_time) if end_time else None

    async with async_session_maker() as db:
        query = select(
            func.count(MessageLog.id).label("total"),
            func.sum(case((MessageLog.success.is_(True), 1), else_=0)).label("successful"),
        )
        query = _apply_filters(
            query, account_ids,
            source=source, status=status,
            start_time=start_dt, end_time=end_dt,
        )
        result = await db.execute(query)
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
    source: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> list[FailureBreakdownItem]:
    """Get failure breakdown by DeliveryStatus (excluding SUCCESS)."""
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    since = utcnow_naive()
    start_dt = _parse_datetime_safe(start_time) if start_time else since
    end_dt = _parse_datetime_safe(end_time) if end_time else None

    async with async_session_maker() as db:
        query = select(
            MessageLog.status,
            func.count(MessageLog.id).label("count"),
        ).where(
            MessageLog.success.is_(False),
        )
        query = _apply_filters(
            query, account_ids,
            source=source,
            start_time=start_dt, end_time=end_dt,
        )
        query = query.group_by(MessageLog.status)
        result = await db.execute(query)
        return [FailureBreakdownItem(status=r[0], count=r[1]) for r in result.all()]


async def get_account_performance(
    identity: Identity,
    days: int = 30,
    source: str | None = None,
    status: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> list[AccountPerformanceItem]:
    """Get delivery performance per authorized account."""
    account_ids = await _resolve_authorized_account_ids(identity)
    if not account_ids:
        return []

    since = utcnow_naive()
    start_dt = _parse_datetime_safe(start_time) if start_time else since
    end_dt = _parse_datetime_safe(end_time) if end_time else None

    async with async_session_maker() as db:
        query = select(
            MessageLog.account_id,
            func.count(MessageLog.id).label("total"),
            func.sum(case((MessageLog.success.is_(True), 1), else_=0)).label("successful"),
        )
        query = _apply_filters(
            query, account_ids,
            source=source, status=status,
            start_time=start_dt, end_time=end_dt,
        )
        query = query.group_by(MessageLog.account_id)
        result = await db.execute(query)
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
    source: str | None = None,
    status: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> list[TimelineItem]:
    """Get delivery timeline grouped by hour or day."""
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    since = utcnow_naive()
    start_dt = _parse_datetime_safe(start_time) if start_time else since
    end_dt = _parse_datetime_safe(end_time) if end_time else None

    async with async_session_maker() as db:
        if interval == "hour":
            date_expr = func.strftime("%Y-%m-%dT%H:00", MessageLog.created_at)
        else:
            date_expr = func.strftime("%Y-%m-%d", MessageLog.created_at)

        query = select(
            date_expr.label("period"),
            func.count(MessageLog.id).label("total"),
            func.sum(case((MessageLog.success.is_(True), 1), else_=0)).label("successful"),
        )
        query = _apply_filters(
            query, account_ids,
            source=source, status=status,
            start_time=start_dt, end_time=end_dt,
        )
        query = query.group_by(date_expr).order_by(date_expr)
        result = await db.execute(query)
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
    source: str | None = None,
    status: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> list[RecentActivityItem]:
    """Get most recent delivery activity for authorized accounts."""
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    limit = min(limit, 200)
    start_dt = _parse_datetime_safe(start_time) if start_time else None
    end_dt = _parse_datetime_safe(end_time) if end_time else None

    async with async_session_maker() as db:
        query = select(MessageLog)
        query = _apply_filters(
            query, account_ids,
            source=source, status=status,
            start_time=start_dt, end_time=end_dt,
        )
        query = query.order_by(MessageLog.created_at.desc()).limit(limit)
        result = await db.execute(query)
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


# ─── Source Analytics ────────────────────────────────────────────────


async def get_source_analytics(
    identity: Identity,
    account_id: str | None = None,
    days: int = 30,
    start_time: str | None = None,
    end_time: str | None = None,
) -> list[SourceAnalyticsItem]:
    """Get delivery analytics grouped by source.

    Sources are derived from actual persisted MessageLog.source values.
    Known sources: broadcast, reply_macro, manual, scheduled.

    SEMANTICS: Attempt-level. Each MessageLog row is counted individually.
    Retries for the same recipient appear as separate rows.
    """
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    since = utcnow_naive()
    start_dt = _parse_datetime_safe(start_time) if start_time else since
    end_dt = _parse_datetime_safe(end_time) if end_time else None

    async with async_session_maker() as db:
        query = select(
            MessageLog.source,
            func.count(MessageLog.id).label("total"),
            func.sum(case((MessageLog.success.is_(True), 1), else_=0)).label("successful"),
        )
        query = _apply_filters(
            query, account_ids,
            start_time=start_dt, end_time=end_dt,
        )
        query = query.group_by(MessageLog.source)
        result = await db.execute(query)

        items = []
        for row in result.all():
            total = row.total or 0
            successful = row.successful or 0
            failed = total - successful
            rate = (successful / total * 100.0) if total > 0 else 0.0
            items.append(SourceAnalyticsItem(
                source=row.source,
                total=total,
                successful=successful,
                failed=failed,
                success_rate=round(rate, 1),
            ))
        return items


# ─── Broadcast Analytics ─────────────────────────────────────────────


async def get_broadcast_analytics(
    identity: Identity,
    account_id: str | None = None,
    days: int = 30,
    start_time: str | None = None,
    end_time: str | None = None,
) -> list[BroadcastAnalyticsItem]:
    """Get per-broadcast delivery analytics.

    Correlates MessageLog records via source='broadcast' and source_id=<broadcast.id>.

    SEMANTICS: Attempt-level within each broadcast. Each MessageLog row
    (including retries) is counted. The broadcast_id is the source_id value
    from MessageLog records where source='broadcast'.

    LIMITATION: Broadcasts with zero MessageLog records (e.g., created but
    never processed) will not appear in results.
    """
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    since = utcnow_naive()
    start_dt = _parse_datetime_safe(start_time) if start_time else since
    end_dt = _parse_datetime_safe(end_time) if end_time else None

    async with async_session_maker() as db:
        query = select(
            MessageLog.source_id,
            func.count(MessageLog.id).label("total"),
            func.sum(case((MessageLog.success.is_(True), 1), else_=0)).label("successful"),
            func.min(MessageLog.created_at).label("first_activity"),
            func.max(MessageLog.created_at).label("latest_activity"),
        ).where(
            MessageLog.source == "broadcast",
            MessageLog.source_id.isnot(None),
        )
        query = _apply_filters(
            query, account_ids,
            start_time=start_dt, end_time=end_dt,
        )
        query = query.group_by(MessageLog.source_id)
        result = await db.execute(query)

        items = []
        for row in result.all():
            total = row.total or 0
            successful = row.successful or 0
            failed = total - successful
            rate = (successful / total * 100.0) if total > 0 else 0.0
            items.append(BroadcastAnalyticsItem(
                broadcast_id=row.source_id,
                total_recipients=total,
                successful=successful,
                failed=failed,
                success_rate=round(rate, 1),
                first_activity=row.first_activity.isoformat() if row.first_activity else None,
                latest_activity=row.latest_activity.isoformat() if row.latest_activity else None,
            ))
        return items


# ─── Failure Intelligence ────────────────────────────────────────────


async def get_failure_intelligence(
    identity: Identity,
    account_id: str | None = None,
    days: int = 30,
    source: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> list[FailureIntelligenceItem]:
    """Enhanced failure analytics with percentages, affected accounts, and latest occurrence.

    Never exposes raw exceptions, API keys, Telegram session secrets, or credentials.
    Only uses the safe error_message field which is already sanitized at persistence time.
    """
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    since = utcnow_naive()
    start_dt = _parse_datetime_safe(start_time) if start_time else since
    end_dt = _parse_datetime_safe(end_time) if end_time else None

    async with async_session_maker() as db:
        # Get total failure count for percentage calculation
        total_query = select(func.count(MessageLog.id)).where(MessageLog.success.is_(False))
        total_query = _apply_filters(
            total_query, account_ids,
            source=source,
            start_time=start_dt, end_time=end_dt,
        )
        total_failures = (await db.execute(total_query)).scalar() or 0

        # Get per-status breakdown with affected accounts and latest occurrence
        query = select(
            MessageLog.status,
            func.count(MessageLog.id).label("count"),
            func.count(func.distinct(MessageLog.account_id)).label("affected_accounts"),
            func.max(MessageLog.created_at).label("latest_occurrence"),
        ).where(MessageLog.success.is_(False))
        query = _apply_filters(
            query, account_ids,
            source=source,
            start_time=start_dt, end_time=end_dt,
        )
        query = query.group_by(MessageLog.status)
        result = await db.execute(query)

        items = []
        for row in result.all():
            count = row.count or 0
            percentage = round((count / total_failures * 100.0), 1) if total_failures > 0 else 0.0
            items.append(FailureIntelligenceItem(
                status=row.status,
                count=count,
                percentage=percentage,
                affected_accounts=row.affected_accounts or 0,
                latest_occurrence=row.latest_occurrence.isoformat() if row.latest_occurrence else None,
            ))
        return items


# ─── Overview Endpoint ───────────────────────────────────────────────


async def get_overview(
    identity: Identity,
    account_id: str | None = None,
    days: int = 30,
    start_time: str | None = None,
    end_time: str | None = None,
) -> OverviewResult:
    """Single aggregated analytics overview.

    Reuses existing service functions to avoid duplicating query logic.
    Executes multiple queries in sequence (not parallel) to keep connection
    usage predictable. Response is bounded by design — top_accounts is
    limited, timeline uses day interval, failure_breakdown uses enhanced
    intelligence.

    Returns None for sections that have no data rather than empty defaults
    to let the caller distinguish "no data" from "not requested".
    """
    summary = await get_summary(
        identity, account_id=account_id, days=days,
        start_time=start_time, end_time=end_time,
    )
    by_source = await get_source_analytics(
        identity, account_id=account_id, days=days,
        start_time=start_time, end_time=end_time,
    )
    top_accounts = await get_account_performance(
        identity, days=days,
        start_time=start_time, end_time=end_time,
    )
    failure_breakdown = await get_failure_intelligence(
        identity, account_id=account_id, days=days,
        start_time=start_time, end_time=end_time,
    )
    timeline = await get_timeline(
        identity, account_id=account_id, days=days,
        interval="day",
        start_time=start_time, end_time=end_time,
    )

    return OverviewResult(
        summary=summary if summary.total_attempted > 0 else None,
        by_source=by_source if by_source else None,
        top_accounts=top_accounts[:5] if top_accounts else None,
        failure_breakdown=failure_breakdown if failure_breakdown else None,
        timeline=timeline if timeline else None,
    )