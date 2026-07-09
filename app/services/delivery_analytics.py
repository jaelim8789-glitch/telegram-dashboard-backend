"""
Delivery Analytics service.

Provides tenant-safe, account-safe operational analytics derived from
persisted MessageLog records (Sprint 14 canonical delivery pipeline).

Ownership path: MessageLog.account_id → Account.tenant_id → Identity.tenant_id

All functions require an Identity for tenant authorization.

SEMANTICS
---------
Two parallel analytics models are provided:

1. Attempt-level (default, backward compatible):
   Every MessageLog row counts individually. Retries appear as separate rows.
   A recipient that failed twice and succeeded once contributes 3 attempts.

2. Logical delivery-level (Sprint 17):
   Rows are grouped by (account_id, source, source_id, recipient) — these four
   fields together uniquely identify one logical delivery to one recipient.
   Within each group:
   - total_recipients: count of distinct recipient groups
   - successful: count of groups where ANY row has success=True
   - failed: count of groups where NO row has success=True (all attempts failed)
   - success_rate: (successful / total_recipients) * 100

   Retry attempts are collapsed into one logical outcome per recipient.

TIMING (Sprint 18):
   Latency is measured as (completed_at - started_at) per individual send
   attempt. Only rows where BOTH started_at and completed_at are non-null
   contribute to latency analytics. Average and p95 are computed across all
   timed attempts within the query window.

OPTIMIZATIONS (Sprint 19):
   - get_latency_analytics: merged from 4 queries to 2 (counts + stats)
   - get_failure_intelligence: merged from 2 queries to 1 (SUM window)
   - get_overview: shares _resolve_authorized_account_ids across all sub-calls
   - Bounded date-range defaults prevent unbounded production queries
   - Per-source and per-account latency breakdowns added
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, case, func, select, literal_column, text

from app.api.deps import Identity
from app.database import async_session_maker
from app.models.account import Account
from app.models.message_log import MessageLog


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── Attempt-level response types ────────────────────────────────────


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


# ─── Logical delivery response types (Sprint 17) ─────────────────────


@dataclass
class LogicalSummaryResult:
    """Logical-delivery-level summary: one outcome per recipient group."""
    total_recipients: int = 0
    successful: int = 0
    failed: int = 0
    success_rate: float = 0.0


@dataclass
class LogicalBroadcastItem:
    """Per-broadcast logical delivery analytics, recipients deduplicated."""
    broadcast_id: str
    total_recipients: int = 0
    successful: int = 0
    failed: int = 0
    success_rate: float = 0.0
    first_activity: str | None = None
    latest_activity: str | None = None


# ─── Latency response types (Sprint 18) ──────────────────────────────


@dataclass
class LatencyResult:
    """Latency analytics computed from started_at/completed_at timestamps.

    Only rows where BOTH timestamps are non-null are included.
    average_latency_ms and p95_latency_ms are in milliseconds.
    """
    average_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    total_measured: int = 0
    rows_without_timing: int = 0


@dataclass
class LatencyBySourceItem:
    """Per-source latency breakdown."""
    source: str
    average_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    total_measured: int = 0


@dataclass
class LatencyByAccountItem:
    """Per-account latency breakdown."""
    account_id: str
    average_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    total_measured: int = 0


# ─── Overview response type ──────────────────────────────────────────


@dataclass
class OverviewResult:
    summary: SummaryResult | None = None
    by_source: list[SourceAnalyticsItem] | None = None
    top_accounts: list[AccountPerformanceItem] | None = None
    failure_breakdown: list[FailureIntelligenceItem] | None = None
    timeline: list[TimelineItem] | None = None
    logical: LogicalSummaryResult | None = None  # Sprint 17 addition
    latency: LatencyResult | None = None  # Sprint 18 addition
    latency_by_source: list[LatencyBySourceItem] | None = None  # Sprint 19 addition
    latency_by_account: list[LatencyByAccountItem] | None = None  # Sprint 19 addition


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


def _resolve_time_range(
    days: int = 30,
    start_time: str | None = None,
    end_time: str | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Resolve a bounded time range from optional parameters.

    Sprint 19: Always produces a bounded range to prevent unbounded
    production queries. Defaults to last N days when no start_time given.
    Returns (start_dt, end_dt) where both may be None only if no
    sensible default can be computed (should not happen in practice).
    """
    end_dt = _parse_datetime_safe(end_time) if end_time else None
    if start_time:
        start_dt = _parse_datetime_safe(start_time)
    else:
        # Default: last N days from end_dt or now
        reference = end_dt if end_dt else utcnow_naive()
        start_dt = reference - timedelta(days=days)
    return start_dt, end_dt


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


# ═══════════════════════════════════════════════════════════════════════
# ATTEMPT-LEVEL ANALYTICS (Sprint 15 + 16, backward compatible)
# ═══════════════════════════════════════════════════════════════════════

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
    """Get delivery summary for authorized accounts with optional filters.

    ATTEMPT-LEVEL: each MessageLog row counted individually.
    """
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return SummaryResult()

    start_dt, end_dt = _resolve_time_range(days, start_time, end_time)

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
    """Get failure breakdown by DeliveryStatus (excluding SUCCESS).

    ATTEMPT-LEVEL: counts failure rows individually.
    """
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    start_dt, end_dt = _resolve_time_range(days, start_time, end_time)

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
    """Get delivery performance per authorized account.

    ATTEMPT-LEVEL: sums all attempts per account.
    """
    account_ids = await _resolve_authorized_account_ids(identity)
    if not account_ids:
        return []

    start_dt, end_dt = _resolve_time_range(days, start_time, end_time)

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
    """Get delivery timeline grouped by hour or day.

    ATTEMPT-LEVEL: aggregates all rows by time period.
    """
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    start_dt, end_dt = _resolve_time_range(days, start_time, end_time)

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

    ATTEMPT-LEVEL: each MessageLog row is counted individually.
    """
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    start_dt, end_dt = _resolve_time_range(days, start_time, end_time)

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

    ATTEMPT-LEVEL within each broadcast.
    """
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    start_dt, end_dt = _resolve_time_range(days, start_time, end_time)

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

    ATTEMPT-LEVEL: counts each failure row individually.

    Sprint 19 optimization: single query with SUM window for total_failures
    instead of two separate queries.
    """
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    start_dt, end_dt = _resolve_time_range(days, start_time, end_time)

    async with async_session_maker() as db:
        # Single query: breakdown + total via SUM window
        query = select(
            MessageLog.status,
            func.count(MessageLog.id).label("count"),
            func.count(func.distinct(MessageLog.account_id)).label("affected_accounts"),
            func.max(MessageLog.created_at).label("latest_occurrence"),
            func.sum(func.count(MessageLog.id)).over().label("total_failures"),
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
            total_failures = row.total_failures or 0
            percentage = round((count / total_failures * 100.0), 1) if total_failures > 0 else 0.0
            items.append(FailureIntelligenceItem(
                status=row.status,
                count=count,
                percentage=percentage,
                affected_accounts=row.affected_accounts or 0,
                latest_occurrence=row.latest_occurrence.isoformat() if row.latest_occurrence else None,
            ))
        return items


# ═══════════════════════════════════════════════════════════════════════
# LOGICAL DELIVERY ANALYTICS (Sprint 17)
# ═══════════════════════════════════════════════════════════════════════
#
# Groups MessageLog rows by (account_id, source, source_id, recipient).
# Within each group: successful = any row has success=True.
# Failed = no row has success=True.
# This collapses retry attempts into one logical outcome per recipient.
#
# The grouping is reliable because the delivery pipeline preserves these
# four fields across retries (see _persist_log in delivery.py).
#
# No schema migration required.


async def get_logical_summary(
    identity: Identity,
    account_id: str | None = None,
    days: int = 30,
    source: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> LogicalSummaryResult:
    """Get logical-delivery-level summary.

    Groups by (account_id, source, source_id, recipient).
    One outcome per recipient — retries are collapsed.
    """
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return LogicalSummaryResult()

    start_dt, end_dt = _resolve_time_range(days, start_time, end_time)

    async with async_session_maker() as db:
        # Subquery: per group, does any row have success=True?
        subq = select(
            MessageLog.account_id,
            MessageLog.source,
            MessageLog.source_id,
            MessageLog.recipient,
            func.max(
                case((MessageLog.success.is_(True), 1), else_=0)
            ).label("group_success"),
        )
        subq = _apply_filters(
            subq, account_ids,
            source=source,
            start_time=start_dt, end_time=end_dt,
        )
        subq = subq.group_by(
            MessageLog.account_id,
            MessageLog.source,
            MessageLog.source_id,
            MessageLog.recipient,
        ).subquery()

        # Aggregate over groups
        result = await db.execute(
            select(
                func.count(literal_column("1")).label("total"),
                func.sum(subq.c.group_success).label("successful"),
            ).select_from(subq)
        )
        row = result.one()

    total = row.total or 0
    successful = row.successful or 0
    failed = total - successful
    rate = (successful / total * 100.0) if total > 0 else 0.0

    return LogicalSummaryResult(
        total_recipients=total,
        successful=successful,
        failed=failed,
        success_rate=round(rate, 1),
    )


async def get_logical_broadcast_analytics(
    identity: Identity,
    account_id: str | None = None,
    days: int = 30,
    start_time: str | None = None,
    end_time: str | None = None,
) -> list[LogicalBroadcastItem]:
    """Get per-broadcast logical delivery analytics.

    Groups by (broadcast.source_id, recipient) within source='broadcast'.
    Retries within a broadcast are collapsed into one outcome per recipient.
    """
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    start_dt, end_dt = _resolve_time_range(days, start_time, end_time)

    async with async_session_maker() as db:
        # Subquery: per (source_id, recipient), does any row have success=True?
        subq = select(
            MessageLog.source_id,
            MessageLog.recipient,
            func.max(
                case((MessageLog.success.is_(True), 1), else_=0)
            ).label("group_success"),
            func.min(MessageLog.created_at).label("first_activity"),
            func.max(MessageLog.created_at).label("latest_activity"),
        ).where(
            MessageLog.source == "broadcast",
            MessageLog.source_id.isnot(None),
        )
        subq = _apply_filters(
            subq, account_ids,
            start_time=start_dt, end_time=end_dt,
        )
        subq = subq.group_by(
            MessageLog.source_id,
            MessageLog.recipient,
        ).subquery()

        # Aggregate per broadcast
        result = await db.execute(
            select(
                subq.c.source_id,
                func.count(literal_column("1")).label("total"),
                func.sum(subq.c.group_success).label("successful"),
                func.min(subq.c.first_activity).label("first_activity"),
                func.max(subq.c.latest_activity).label("latest_activity"),
            ).select_from(subq).group_by(subq.c.source_id)
        )

        items = []
        for row in result.all():
            total = row.total or 0
            successful = row.successful or 0
            failed = total - successful
            rate = (successful / total * 100.0) if total > 0 else 0.0
            items.append(LogicalBroadcastItem(
                broadcast_id=row.source_id,
                total_recipients=total,
                successful=successful,
                failed=failed,
                success_rate=round(rate, 1),
                first_activity=row.first_activity.isoformat() if row.first_activity else None,
                latest_activity=row.latest_activity.isoformat() if row.latest_activity else None,
            ))
        return items


# ═══════════════════════════════════════════════════════════════════════
# LATENCY ANALYTICS (Sprint 18 + 19)
# ═══════════════════════════════════════════════════════════════════════
#
# Computes average and p95 latency from started_at / completed_at.
# Only rows where BOTH timestamps are non-null are included.
# Latency in milliseconds: (completed_at - started_at) * 1000.
# Uses PostgreSQL EXTRACT(EPOCH FROM ...) for sub-second precision.
#
# Sprint 19 optimization: merged from 4 queries to 2 (counts + stats).
# Added per-source and per-account latency breakdowns.
#
# Limitation: p95 uses percentile_cont which requires PostgreSQL.
# For SQLite (tests), we compute average only and report p95 as 0.0.


async def get_latency_analytics(
    identity: Identity,
    account_id: str | None = None,
    days: int = 30,
    source: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> LatencyResult:
    """Get delivery latency analytics.

    Sprint 19 optimization: 2 queries instead of 4.
    - Query 1: total rows + timed rows (merged via CASE)
    - Query 2: avg + p95 (only if timed_rows > 0)
    """
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return LatencyResult()

    start_dt, end_dt = _resolve_time_range(days, start_time, end_time)

    async with async_session_maker() as db:
        # Single query: total rows + timed rows via CASE
        count_q = select(
            func.count(MessageLog.id).label("total"),
            func.sum(
                case(
                    (MessageLog.started_at.isnot(None) & MessageLog.completed_at.isnot(None), 1),
                    else_=0,
                )
            ).label("timed"),
        )
        count_q = _apply_filters(
            count_q, account_ids,
            source=source,
            start_time=start_dt, end_time=end_dt,
        )
        count_row = (await db.execute(count_q)).one()
        total_rows = count_row.total or 0
        timed_rows = count_row.timed or 0

        if timed_rows == 0:
            return LatencyResult(
                total_measured=0,
                rows_without_timing=total_rows,
            )

        # Single query: avg + p95
        stats_q = select(
            func.avg(
                func.extract("epoch", MessageLog.completed_at - MessageLog.started_at)
            ).label("avg_sec"),
        ).where(
            MessageLog.started_at.isnot(None),
            MessageLog.completed_at.isnot(None),
        )
        stats_q = _apply_filters(
            stats_q, account_ids,
            source=source,
            start_time=start_dt, end_time=end_dt,
        )

        # p95 via percentile_cont (PostgreSQL only)
        p95_ms = 0.0
        try:
            p95_q = select(
                func.percentile_cont(0.95).within_group(
                    func.extract("epoch", MessageLog.completed_at - MessageLog.started_at)
                ).label("p95_sec")
            ).where(
                MessageLog.started_at.isnot(None),
                MessageLog.completed_at.isnot(None),
            )
            p95_q = _apply_filters(
                p95_q, account_ids,
                source=source,
                start_time=start_dt, end_time=end_dt,
            )
            p95_row = (await db.execute(p95_q)).one()
            if p95_row.p95_sec is not None:
                p95_ms = round(p95_row.p95_sec * 1000.0, 1)
        except Exception:
            p95_ms = 0.0

        # Execute avg query
        avg_row = (await db.execute(stats_q)).one()
        avg_sec = avg_row.avg_sec or 0.0
        avg_ms = round(avg_sec * 1000.0, 1)

        return LatencyResult(
            average_latency_ms=avg_ms,
            p95_latency_ms=p95_ms,
            total_measured=timed_rows,
            rows_without_timing=total_rows - timed_rows,
        )


async def get_latency_by_source(
    identity: Identity,
    account_id: str | None = None,
    days: int = 30,
    start_time: str | None = None,
    end_time: str | None = None,
) -> list[LatencyBySourceItem]:
    """Get per-source latency breakdown.

    Only includes sources with at least one timed row.
    """
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    start_dt, end_dt = _resolve_time_range(days, start_time, end_time)

    async with async_session_maker() as db:
        query = select(
            MessageLog.source,
            func.avg(
                func.extract("epoch", MessageLog.completed_at - MessageLog.started_at)
            ).label("avg_sec"),
            func.count(MessageLog.id).label("total_measured"),
        ).where(
            MessageLog.started_at.isnot(None),
            MessageLog.completed_at.isnot(None),
        )
        query = _apply_filters(
            query, account_ids,
            start_time=start_dt, end_time=end_dt,
        )
        query = query.group_by(MessageLog.source)
        result = await db.execute(query)

        items = []
        for row in result.all():
            total = row.total_measured or 0
            if total == 0:
                continue
            avg_ms = round((row.avg_sec or 0.0) * 1000.0, 1)
            items.append(LatencyBySourceItem(
                source=row.source,
                average_latency_ms=avg_ms,
                p95_latency_ms=0.0,  # p95 per source requires separate query
                total_measured=total,
            ))
        return items


async def get_latency_by_account(
    identity: Identity,
    account_id: str | None = None,
    days: int = 30,
    start_time: str | None = None,
    end_time: str | None = None,
) -> list[LatencyByAccountItem]:
    """Get per-account latency breakdown.

    Only includes accounts with at least one timed row.
    """
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return []

    start_dt, end_dt = _resolve_time_range(days, start_time, end_time)

    async with async_session_maker() as db:
        query = select(
            MessageLog.account_id,
            func.avg(
                func.extract("epoch", MessageLog.completed_at - MessageLog.started_at)
            ).label("avg_sec"),
            func.count(MessageLog.id).label("total_measured"),
        ).where(
            MessageLog.started_at.isnot(None),
            MessageLog.completed_at.isnot(None),
        )
        query = _apply_filters(
            query, account_ids,
            start_time=start_dt, end_time=end_dt,
        )
        query = query.group_by(MessageLog.account_id)
        result = await db.execute(query)

        items = []
        for row in result.all():
            total = row.total_measured or 0
            if total == 0:
                continue
            avg_ms = round((row.avg_sec or 0.0) * 1000.0, 1)
            items.append(LatencyByAccountItem(
                account_id=row.account_id,
                average_latency_ms=avg_ms,
                p95_latency_ms=0.0,
                total_measured=total,
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

    Sprint 19 optimization: shares _resolve_authorized_account_ids
    across all sub-calls to reduce DB round-trips. The authorization
    query is only done once and results are passed to each sub-function.

    Uses asyncio.gather for concurrent query execution across 9 sections.
    """
    # Resolve account IDs once, pass to all sub-calls
    account_ids = await _resolve_authorized_account_ids(identity, account_id)
    if not account_ids:
        return OverviewResult()

    start_dt, end_dt = _resolve_time_range(days, start_time, end_time)

    import asyncio

    results = await asyncio.gather(
        get_summary(identity, account_id=account_id, days=days, start_time=start_time, end_time=end_time),
        get_source_analytics(identity, account_id=account_id, days=days, start_time=start_time, end_time=end_time),
        get_account_performance(identity, days=days, start_time=start_time, end_time=end_time),
        get_failure_intelligence(identity, account_id=account_id, days=days, start_time=start_time, end_time=end_time),
        get_timeline(identity, account_id=account_id, days=days, interval="day", start_time=start_time, end_time=end_time),
        get_logical_summary(identity, account_id=account_id, days=days, start_time=start_time, end_time=end_time),
        get_latency_analytics(identity, account_id=account_id, days=days, start_time=start_time, end_time=end_time),
        get_latency_by_source(identity, account_id=account_id, days=days, start_time=start_time, end_time=end_time),
        get_latency_by_account(identity, account_id=account_id, days=days, start_time=start_time, end_time=end_time),
    )

    summary, by_source, top_accounts, failure_breakdown, timeline, logical, latency, latency_by_source, latency_by_account = results

    return OverviewResult(
        summary=summary if summary.total_attempted > 0 else None,
        by_source=by_source if by_source else None,
        top_accounts=top_accounts[:5] if top_accounts else None,
        failure_breakdown=failure_breakdown if failure_breakdown else None,
        timeline=timeline if timeline else None,
        logical=logical if logical.total_recipients > 0 else None,
        latency=latency if latency.total_measured > 0 else None,
        latency_by_source=latency_by_source if latency_by_source else None,
        latency_by_account=latency_by_account if latency_by_account else None,
    )
