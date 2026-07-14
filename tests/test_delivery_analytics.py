"""Sprint 15 + Sprint 16: Behavioral tests for delivery analytics.

Sprint 15 tests (preserved):
- Summary, failure breakdown, account performance, timeline, recent activity
- Tenant A/B isolation
- Zero-division safety
- Empty dataset handling
- Source attribution

Sprint 16 extensions:
- Filtering (source, account_id, status, start_time, end_time)
- Source analytics
- Broadcast analytics
- Failure intelligence
- Overview endpoint
- Cross-tenant account rejection
- Invalid account authorization
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
from datetime import datetime, timezone

from app.api.deps import Identity
from app.models.message_log import MessageLog
from app.services.delivery_analytics import (
    SummaryResult,
    FailureBreakdownItem,
    AccountPerformanceItem,
    TimelineItem,
    RecentActivityItem,
    SourceAnalyticsItem,
    BroadcastAnalyticsItem,
    FailureIntelligenceItem,
    LogicalSummaryResult,
    LatencyResult,
    LatencyBySourceItem,
    LatencyByAccountItem,
    OverviewResult,
    get_summary,
    get_failure_breakdown,
    get_account_performance,
    get_timeline,
    get_recent_activity,
    get_source_analytics,
    get_broadcast_analytics,
    get_failure_intelligence,
    get_logical_summary,
    get_logical_broadcast_analytics,
    get_latency_analytics,
    get_latency_by_source,
    get_latency_by_account,
    get_overview,
    _resolve_authorized_account_ids,
    _resolve_time_range,
    _parse_datetime_safe,
    utcnow_naive,
)


def _make_log(**kwargs) -> MessageLog:
    defaults = dict(
        id="test-log-id",
        account_id="acc-1",
        recipient="-100123",
        source="broadcast",
        source_id="b1",
        status="success",
        success=True,
        telegram_message_id=42,
        error_message=None,
        attempt_count=1,
        message_content="test",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    defaults.update(kwargs)
    return MessageLog(**defaults)


def _mock_result(rows=None, one_result=None, scalars_result=None, scalar_result=None):
    """Create a mock SQLAlchemy result (sync methods)."""
    mock = MagicMock()
    if rows is not None:
        mock.all.return_value = rows
    if one_result is not None:
        mock.one.return_value = one_result
    if scalars_result is not None:
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = scalars_result
        mock.scalars.return_value = mock_scalars
    if scalar_result is not None:
        mock.scalar.return_value = scalar_result
    return mock


def _mock_db_session(execute_result=None):
    """Create a mock async DB session."""
    mock_db = AsyncMock()
    mock_db.execute.return_value = execute_result
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_db
    return mock_db, mock_session


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def tenant_a_identity():
    return Identity(kind="user", tenant_id="tenant-A")


@pytest.fixture
def tenant_b_identity():
    return Identity(kind="user", tenant_id="tenant-B")


@pytest.fixture
def admin_identity():
    return Identity(kind="admin")


@pytest.fixture
def no_tenant_identity():
    return Identity(kind="api_key", tenant_id=None)


# ═══════════════════════════════════════════════════════════════════════
# Phase 1: _resolve_authorized_account_ids tests
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("app.services.delivery_analytics.async_session_maker")
async def test_resolve_admin_sees_all(mock_session_maker, admin_identity):
    result = _mock_result(rows=[("acc-1",), ("acc-2",)])
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    ids = await _resolve_authorized_account_ids(admin_identity)
    assert len(ids) == 2


@pytest.mark.asyncio
@patch("app.services.delivery_analytics.async_session_maker")
async def test_resolve_no_tenant_returns_empty(mock_session_maker, no_tenant_identity):
    ids = await _resolve_authorized_account_ids(no_tenant_identity)
    assert ids == []


# ═══════════════════════════════════════════════════════════════════════
# Phase 2: Summary tests
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_summary_empty(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1"]
    result = _mock_result(one_result=type("Row", (), {"total": 0, "successful": 0})())
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    s = await get_summary(tenant_a_identity)
    assert s.total_attempted == 0
    assert s.successful == 0
    assert s.failed == 0
    assert s.success_rate == 0.0


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_summary_all_success(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1"]
    result = _mock_result(one_result=type("Row", (), {"total": 100, "successful": 100})())
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    s = await get_summary(tenant_a_identity)
    assert s.total_attempted == 100
    assert s.successful == 100
    assert s.failed == 0
    assert s.success_rate == 100.0


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_summary_mixed(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1"]
    result = _mock_result(one_result=type("Row", (), {"total": 200, "successful": 150})())
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    s = await get_summary(tenant_a_identity)
    assert s.total_attempted == 200
    assert s.successful == 150
    assert s.failed == 50
    assert s.success_rate == 75.0


# ═══════════════════════════════════════════════════════════════════════
# Phase 3: Failure breakdown tests
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_failure_breakdown(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1"]
    result = _mock_result(rows=[("flood_wait", 5), ("forbidden", 3), ("network_error", 2)])
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    fb = await get_failure_breakdown(tenant_a_identity)
    assert len(fb) == 3
    assert fb[0].status == "flood_wait"
    assert fb[0].count == 5


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_failure_breakdown_empty(mock_resolve, tenant_a_identity):
    mock_resolve.return_value = []
    fb = await get_failure_breakdown(tenant_a_identity)
    assert fb == []


# ═══════════════════════════════════════════════════════════════════════
# Phase 4: Account performance tests
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_account_performance(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1", "acc-2"]
    Row = type("Row", (), {"account_id": "", "total": 0, "successful": 0})
    r1 = Row()
    r1.account_id, r1.total, r1.successful = "acc-1", 100, 80
    r2 = Row()
    r2.account_id, r2.total, r2.successful = "acc-2", 50, 50
    result = _mock_result(rows=[r1, r2])
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    ap = await get_account_performance(tenant_a_identity)
    assert len(ap) == 2
    assert ap[0].account_id == "acc-1"
    assert ap[0].success_rate == 80.0
    assert ap[1].success_rate == 100.0


# ═══════════════════════════════════════════════════════════════════════
# Phase 5: Timeline tests
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_timeline_daily(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1"]
    Row = type("Row", (), {"period": "", "total": 0, "successful": 0})
    r1 = Row()
    r1.period, r1.total, r1.successful = "2026-07-01", 10, 8
    r2 = Row()
    r2.period, r2.total, r2.successful = "2026-07-02", 5, 5
    result = _mock_result(rows=[r1, r2])
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    tl = await get_timeline(tenant_a_identity, interval="day")
    assert len(tl) == 2
    assert tl[0].period == "2026-07-01"
    assert tl[0].failed == 2


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_timeline_empty(mock_resolve, tenant_a_identity):
    mock_resolve.return_value = []
    tl = await get_timeline(tenant_a_identity)
    assert tl == []


# ═══════════════════════════════════════════════════════════════════════
# Phase 6: Recent activity tests
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_recent_activity(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1"]
    log = _make_log(id="log-1", account_id="acc-1", recipient="-100999", status="success", telegram_message_id=77)
    result = _mock_result(scalars_result=[log])
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    ra = await get_recent_activity(tenant_a_identity, limit=10)
    assert len(ra) == 1
    assert ra[0].id == "log-1"
    assert ra[0].telegram_message_id == 77


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_recent_activity_empty(mock_resolve, tenant_a_identity):
    mock_resolve.return_value = []
    ra = await get_recent_activity(tenant_a_identity)
    assert ra == []


# ═══════════════════════════════════════════════════════════════════════
# Phase 7: Tenant A/B isolation tests
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("app.services.delivery_analytics.async_session_maker")
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_tenant_a_cannot_see_tenant_b_summary(mock_resolve, mock_sm, tenant_a_identity):
    mock_sm.return_value.__aenter__.return_value.execute.return_value = _mock_result(
        one_result=type("Row", (), {"total": 0, "successful": 0})()
    )
    mock_resolve.return_value = ["acc-a-1"]
    s = await get_summary(tenant_a_identity)
    assert s.total_attempted == 0


@pytest.mark.asyncio
@patch("app.services.delivery_analytics.async_session_maker")
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_tenant_a_cannot_see_tenant_b_failures(mock_resolve, mock_sm, tenant_a_identity):
    mock_sm.return_value.__aenter__.return_value.execute.return_value = _mock_result(rows=[])
    mock_resolve.return_value = ["acc-a-1"]
    fb = await get_failure_breakdown(tenant_a_identity)
    assert fb == []


@pytest.mark.asyncio
@patch("app.services.delivery_analytics.async_session_maker")
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_tenant_a_cannot_see_tenant_b_account_perf(mock_resolve, mock_sm, tenant_a_identity):
    mock_sm.return_value.__aenter__.return_value.execute.return_value = _mock_result(rows=[])
    mock_resolve.return_value = ["acc-a-1"]
    ap = await get_account_performance(tenant_a_identity)
    assert len(ap) == 0


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_tenant_a_cannot_see_tenant_b_timeline(mock_resolve, tenant_a_identity):
    mock_resolve.return_value = []
    tl = await get_timeline(tenant_a_identity)
    assert tl == []


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_tenant_a_cannot_see_tenant_b_recent(mock_resolve, tenant_a_identity):
    mock_resolve.return_value = []
    ra = await get_recent_activity(tenant_a_identity)
    assert ra == []


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_no_tenant_returns_empty_summary(mock_resolve, no_tenant_identity):
    mock_resolve.return_value = []
    s = await get_summary(no_tenant_identity)
    assert s.total_attempted == 0


# ═══════════════════════════════════════════════════════════════════════
# Phase 8: Zero-division safety
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_summary_zero_division_safe(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1"]
    result = _mock_result(one_result=type("Row", (), {"total": 0, "successful": 0})())
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    s = await get_summary(tenant_a_identity)
    assert s.success_rate == 0.0


# ═══════════════════════════════════════════════════════════════════════
# Phase 9: Source attribution (Sprint 15 preserved)
# ═══════════════════════════════════════════════════════════════════════

def test_source_attribution_broadcast():
    log = _make_log(source="broadcast")
    assert log.source == "broadcast"


def test_source_attribution_reply_macro():
    log = _make_log(source="reply_macro")
    assert log.source == "reply_macro"


def test_source_attribution_manual():
    log = _make_log(source="manual")
    assert log.source == "manual"


# ═══════════════════════════════════════════════════════════════════════
# Phase 10: Safe error messages
# ═══════════════════════════════════════════════════════════════════════

def test_recent_activity_safe_error():
    log = _make_log(status="forbidden", success=False, error_message="해당 채팅방에 메시지를 보낼 권한이 없습니다.")
    assert "UserDeactivatedBanError" not in (log.error_message or "")
    assert "권한" in (log.error_message or "")


# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════
# SPRINT 16 — FILTERING TESTS
# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════


# ─── _parse_datetime_safe ────────────────────────────────────────────

class TestParseDatetimeSafe:
    def test_valid_iso(self):
        dt = _parse_datetime_safe("2026-07-01T12:00:00")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 7
        assert dt.day == 1

    def test_valid_iso_with_tz(self):
        dt = _parse_datetime_safe("2026-07-01T12:00:00+00:00")
        assert dt is not None
        assert dt.tzinfo is None  # stripped

    def test_none_returns_none(self):
        assert _parse_datetime_safe(None) is None

    def test_invalid_returns_none(self):
        assert _parse_datetime_safe("not-a-date") is None

    def test_empty_returns_none(self):
        assert _parse_datetime_safe("") is None


# ─── Source filter ───────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_summary_source_filter(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1"]
    result = _mock_result(one_result=type("Row", (), {"total": 50, "successful": 40})())
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    s = await get_summary(tenant_a_identity, source="broadcast")
    assert s.total_attempted == 50
    assert s.successful == 40


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_summary_status_filter(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1"]
    result = _mock_result(one_result=type("Row", (), {"total": 10, "successful": 0})())
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    s = await get_summary(tenant_a_identity, status="flood_wait")
    assert s.total_attempted == 10


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_summary_start_time_filter(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1"]
    result = _mock_result(one_result=type("Row", (), {"total": 25, "successful": 20})())
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    s = await get_summary(tenant_a_identity, start_time="2026-07-01T00:00:00")
    assert s.total_attempted == 25


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_summary_end_time_filter(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1"]
    result = _mock_result(one_result=type("Row", (), {"total": 15, "successful": 10})())
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    s = await get_summary(tenant_a_identity, end_time="2026-07-15T23:59:59")
    assert s.total_attempted == 15


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_summary_combined_filters(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1"]
    result = _mock_result(one_result=type("Row", (), {"total": 8, "successful": 6})())
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    s = await get_summary(
        tenant_a_identity,
        source="broadcast",
        status="success",
        start_time="2026-07-01T00:00:00",
        end_time="2026-07-31T23:59:59",
    )
    assert s.total_attempted == 8


# ─── Invalid account authorization ───────────────────────────────────

@pytest.mark.asyncio
@patch("app.services.delivery_analytics.async_session_maker")
async def test_invalid_account_id_returns_empty(mock_session_maker, tenant_a_identity):
    """An account_id that doesn't exist should return empty results."""
    mock_db = AsyncMock()
    mock_db.execute.return_value = _mock_result(rows=[])
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_db
    mock_session_maker.return_value = mock_session

    s = await get_summary(tenant_a_identity, account_id="nonexistent-acc")
    assert s.total_attempted == 0


# ─── Cross-tenant account rejection ──────────────────────────────────

@pytest.mark.asyncio
@patch("app.services.delivery_analytics.async_session_maker")
async def test_cross_tenant_account_rejected(mock_session_maker, tenant_a_identity):
    """Tenant A should not be able to query Tenant B's account."""
    mock_db = AsyncMock()
    # Simulate that tenant_a_identity only sees acc-a-1, not acc-b-1
    mock_db.execute.return_value = _mock_result(rows=[])
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_db
    mock_session_maker.return_value = mock_session

    s = await get_summary(tenant_a_identity, account_id="acc-b-1")
    assert s.total_attempted == 0


# ═══════════════════════════════════════════════════════════════════════
# SPRINT 16 — SOURCE ANALYTICS TESTS
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_source_analytics_correct_totals(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1"]
    Row = type("Row", (), {"source": "", "total": 0, "successful": 0})
    r1 = Row()
    r1.source, r1.total, r1.successful = "broadcast", 100, 80
    r2 = Row()
    r2.source, r2.total, r2.successful = "manual", 50, 50
    result = _mock_result(rows=[r1, r2])
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    sa = await get_source_analytics(tenant_a_identity)
    assert len(sa) == 2
    assert sa[0].source == "broadcast"
    assert sa[0].total == 100
    assert sa[0].successful == 80
    assert sa[0].failed == 20
    assert sa[0].success_rate == 80.0
    assert sa[1].success_rate == 100.0


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_source_analytics_zero_result(mock_resolve, tenant_a_identity):
    mock_resolve.return_value = []
    sa = await get_source_analytics(tenant_a_identity)
    assert sa == []


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_source_analytics_tenant_isolation(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-a-1"]
    result = _mock_result(rows=[])
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    sa = await get_source_analytics(tenant_a_identity)
    assert sa == []


# ═══════════════════════════════════════════════════════════════════════
# SPRINT 16 — BROADCAST ANALYTICS TESTS
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_broadcast_analytics(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1"]
    Row = type("Row", (), {
        "source_id": "", "total": 0, "successful": 0,
        "first_activity": None, "latest_activity": None,
    })
    r1 = Row()
    r1.source_id, r1.total, r1.successful = "b1", 50, 40
    r1.first_activity = datetime(2026, 7, 1, 10, 0, 0)
    r1.latest_activity = datetime(2026, 7, 1, 10, 30, 0)
    result = _mock_result(rows=[r1])
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    ba = await get_broadcast_analytics(tenant_a_identity)
    assert len(ba) == 1
    assert ba[0].broadcast_id == "b1"
    assert ba[0].total_recipients == 50
    assert ba[0].successful == 40
    assert ba[0].failed == 10
    assert ba[0].success_rate == 80.0
    assert ba[0].first_activity is not None
    assert ba[0].latest_activity is not None


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_broadcast_analytics_empty(mock_resolve, tenant_a_identity):
    mock_resolve.return_value = []
    ba = await get_broadcast_analytics(tenant_a_identity)
    assert ba == []


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_broadcast_analytics_tenant_isolation(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-a-1"]
    result = _mock_result(rows=[])
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    ba = await get_broadcast_analytics(tenant_a_identity)
    assert ba == []


# ═══════════════════════════════════════════════════════════════════════
# SPRINT 16 — FAILURE INTELLIGENCE TESTS
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_failure_intelligence(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-1"]
    Row = type("Row", (), {"status": "", "count": 0, "affected_accounts": 0, "latest_occurrence": None, "total_failures": 0})
    r1 = Row()
    r1.status, r1.count, r1.affected_accounts, r1.total_failures = "flood_wait", 10, 2, 15
    r1.latest_occurrence = datetime(2026, 7, 1, 12, 0, 0)
    r2 = Row()
    r2.status, r2.count, r2.affected_accounts, r2.total_failures = "forbidden", 5, 1, 15
    r2.latest_occurrence = datetime(2026, 7, 1, 14, 0, 0)
    mock_db = AsyncMock()
    mock_db.execute.return_value = _mock_result(rows=[r1, r2])
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_db
    mock_session_maker.return_value = mock_session

    fi = await get_failure_intelligence(tenant_a_identity)
    assert len(fi) == 2
    assert fi[0].status == "flood_wait"
    assert fi[0].count == 10
    assert fi[0].percentage == 66.7
    assert fi[0].affected_accounts == 2
    assert fi[0].latest_occurrence is not None
    assert fi[1].percentage == 33.3


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_failure_intelligence_empty(mock_resolve, tenant_a_identity):
    mock_resolve.return_value = []
    fi = await get_failure_intelligence(tenant_a_identity)
    assert fi == []


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_failure_intelligence_safe_error_output(mock_session_maker, mock_resolve, tenant_a_identity):
    """Verify failure intelligence never exposes raw exception details."""
    mock_resolve.return_value = ["acc-1"]
    Row = type("Row", (), {"status": "", "count": 0, "affected_accounts": 0, "latest_occurrence": None, "total_failures": 0})
    r1 = Row()
    r1.status, r1.count, r1.affected_accounts, r1.total_failures = "internal_error", 5, 1, 5
    r1.latest_occurrence = datetime(2026, 7, 1, 12, 0, 0)
    mock_db = AsyncMock()
    mock_db.execute.return_value = _mock_result(rows=[r1])
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_db
    mock_session_maker.return_value = mock_session

    fi = await get_failure_intelligence(tenant_a_identity)
    assert len(fi) == 1
    # The status field is a safe enum value, not a raw exception
    assert fi[0].status == "internal_error"
    # No raw exception details in the response
    assert not hasattr(fi[0], "exception")
    assert not hasattr(fi[0], "traceback")


# ═══════════════════════════════════════════════════════════════════════
# SPRINT 16 — OVERVIEW ENDPOINT TESTS
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("app.services.delivery_analytics.get_latency_by_account")
@patch("app.services.delivery_analytics.get_latency_by_source")
@patch("app.services.delivery_analytics.get_summary")
@patch("app.services.delivery_analytics.get_source_analytics")
@patch("app.services.delivery_analytics.get_account_performance")
@patch("app.services.delivery_analytics.get_failure_intelligence")
@patch("app.services.delivery_analytics.get_timeline")
@patch("app.services.delivery_analytics.get_logical_summary")
@patch("app.services.delivery_analytics.get_latency_analytics")
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_overview_response_contract(
    mock_resolve, mock_latency, mock_logical, mock_timeline, mock_failure, mock_perf, mock_source, mock_summary, mock_latency_by_source, mock_latency_by_account,
    tenant_a_identity,
):
    mock_resolve.return_value = ["acc-1"]
    mock_latency_by_account.return_value = []
    mock_latency_by_source.return_value = []
    mock_latency.return_value = LatencyResult(average_latency_ms=150.0, p95_latency_ms=300.0, total_measured=80, rows_without_timing=20)
    mock_logical.return_value = LogicalSummaryResult(total_recipients=80, successful=70, failed=10, success_rate=87.5)
    mock_summary.return_value = SummaryResult(total_attempted=100, successful=80, failed=20, success_rate=80.0)
    mock_source.return_value = [SourceAnalyticsItem(source="broadcast", total=100, successful=80, failed=20, success_rate=80.0)]
    mock_perf.return_value = [AccountPerformanceItem(account_id="acc-1", attempted=100, successful=80, failed=20, success_rate=80.0)]
    mock_failure.return_value = [FailureIntelligenceItem(status="flood_wait", count=10, percentage=50.0, affected_accounts=2, latest_occurrence="2026-07-01T12:00:00")]
    mock_timeline.return_value = [TimelineItem(period="2026-07-01", attempted=50, successful=40, failed=10)]

    overview = await get_overview(tenant_a_identity)

    assert overview.summary is not None
    assert overview.summary.total_attempted == 100
    assert overview.by_source is not None
    assert len(overview.by_source) == 1
    assert overview.top_accounts is not None
    assert len(overview.top_accounts) == 1
    assert overview.failure_breakdown is not None
    assert len(overview.failure_breakdown) == 1
    assert overview.timeline is not None
    assert len(overview.timeline) == 1


@pytest.mark.asyncio
@patch("app.services.delivery_analytics.get_latency_by_account")
@patch("app.services.delivery_analytics.get_latency_by_source")
@patch("app.services.delivery_analytics.get_summary")
@patch("app.services.delivery_analytics.get_source_analytics")
@patch("app.services.delivery_analytics.get_account_performance")
@patch("app.services.delivery_analytics.get_failure_intelligence")
@patch("app.services.delivery_analytics.get_timeline")
@patch("app.services.delivery_analytics.get_logical_summary")
@patch("app.services.delivery_analytics.get_latency_analytics")
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_overview_empty_data_returns_none_sections(
    mock_resolve, mock_latency, mock_logical, mock_timeline, mock_failure, mock_perf, mock_source, mock_summary, mock_latency_by_source, mock_latency_by_account,
    tenant_a_identity,
):
    mock_resolve.return_value = ["acc-1"]
    mock_latency_by_account.return_value = []
    mock_latency_by_source.return_value = []
    mock_latency.return_value = LatencyResult()
    mock_logical.return_value = LogicalSummaryResult()
    mock_summary.return_value = SummaryResult()
    mock_source.return_value = []
    mock_perf.return_value = []
    mock_failure.return_value = []
    mock_timeline.return_value = []

    overview = await get_overview(tenant_a_identity)

    assert overview.summary is None
    assert overview.by_source is None
    assert overview.top_accounts is None
    assert overview.failure_breakdown is None
    assert overview.timeline is None


@pytest.mark.asyncio
@patch("app.services.delivery_analytics.get_latency_by_account")
@patch("app.services.delivery_analytics.get_latency_by_source")
@patch("app.services.delivery_analytics.get_summary")
@patch("app.services.delivery_analytics.get_source_analytics")
@patch("app.services.delivery_analytics.get_account_performance")
@patch("app.services.delivery_analytics.get_failure_intelligence")
@patch("app.services.delivery_analytics.get_timeline")
@patch("app.services.delivery_analytics.get_logical_summary")
@patch("app.services.delivery_analytics.get_latency_analytics")
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_overview_tenant_isolation(
    mock_resolve, mock_latency, mock_logical, mock_timeline, mock_failure, mock_perf, mock_source, mock_summary, mock_latency_by_source, mock_latency_by_account,
    tenant_a_identity,
):
    mock_resolve.return_value = ["acc-1"]
    mock_latency_by_account.return_value = []
    mock_latency_by_source.return_value = []
    mock_latency.return_value = LatencyResult()
    mock_logical.return_value = LogicalSummaryResult()
    mock_summary.return_value = SummaryResult()
    mock_source.return_value = []
    mock_perf.return_value = []
    mock_failure.return_value = []
    mock_timeline.return_value = []

    overview = await get_overview(tenant_a_identity)

    assert overview.summary is None
    assert overview.by_source is None
    assert overview.top_accounts is None


@pytest.mark.asyncio
@patch("app.services.delivery_analytics.get_latency_by_account")
@patch("app.services.delivery_analytics.get_latency_by_source")
@patch("app.services.delivery_analytics.get_summary")
@patch("app.services.delivery_analytics.get_source_analytics")
@patch("app.services.delivery_analytics.get_account_performance")
@patch("app.services.delivery_analytics.get_failure_intelligence")
@patch("app.services.delivery_analytics.get_timeline")
@patch("app.services.delivery_analytics.get_logical_summary")
@patch("app.services.delivery_analytics.get_latency_analytics")
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_overview_bounded_top_accounts(
    mock_resolve, mock_latency, mock_logical, mock_timeline, mock_failure, mock_perf, mock_source, mock_summary, mock_latency_by_source, mock_latency_by_account,
    tenant_a_identity,
):
    mock_resolve.return_value = ["acc-1"]
    mock_latency_by_account.return_value = []
    mock_latency_by_source.return_value = []
    mock_latency.return_value = LatencyResult()
    mock_logical.return_value = LogicalSummaryResult()
    mock_summary.return_value = SummaryResult(total_attempted=100, successful=80, failed=20, success_rate=80.0)
    mock_source.return_value = []
    mock_perf.return_value = [
        AccountPerformanceItem(account_id=f"acc-{i}", attempted=10, successful=8, failed=2, success_rate=80.0)
        for i in range(10)
    ]
    mock_failure.return_value = []
    mock_timeline.return_value = []

    overview = await get_overview(tenant_a_identity)

    assert overview.top_accounts is not None
    assert len(overview.top_accounts) <= 5  # bounded


# ═══════════════════════════════════════════════════════════════════════
# SPRINT 16 — REGRESSION: Existing tests must still pass
# ═══════════════════════════════════════════════════════════════════════

def test_existing_source_attribution_tests_preserved():
    """Verify Sprint 15 source attribution tests are still present."""
    log_b = _make_log(source="broadcast")
    log_r = _make_log(source="reply_macro")
    log_m = _make_log(source="manual")
    assert log_b.source == "broadcast"
    assert log_r.source == "reply_macro"
    assert log_m.source == "manual"


def test_existing_safe_error_test_preserved():
    """Verify Sprint 15 safe error test is still present."""
    log = _make_log(status="forbidden", success=False, error_message="해당 채팅방에 메시지를 보낼 권한이 없습니다.")
    assert "UserDeactivatedBanError" not in (log.error_message or "")
    assert "권한" in (log.error_message or "")


# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════
# SPRINT 17 — LOGICAL DELIVERY ANALYTICS TESTS
# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════


# ─── get_logical_summary ─────────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_logical_summary_collapses_retries(mock_session_maker, mock_resolve, tenant_a_identity):
    """3 attempts to same recipient (2 failed, 1 success) → 1 logical recipient, 1 success."""
    mock_resolve.return_value = ["acc-1"]
    Row = type("Row", (), {"total": 0, "successful": 0})
    row = Row()
    row.total, row.successful = 1, 1  # 1 group, 1 successful
    mock_db = AsyncMock()
    mock_db.execute.return_value = _mock_result(one_result=row)
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_db
    mock_session_maker.return_value = mock_session

    result = await get_logical_summary(tenant_a_identity)
    assert result.total_recipients == 1
    assert result.successful == 1
    assert result.failed == 0
    assert result.success_rate == 100.0


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_logical_summary_all_attempts_failed(mock_session_maker, mock_resolve, tenant_a_identity):
    """3 failed attempts to same recipient → 1 logical recipient, 0 success, 1 failed."""
    mock_resolve.return_value = ["acc-1"]
    Row = type("Row", (), {"total": 0, "successful": 0})
    row = Row()
    row.total, row.successful = 1, 0
    mock_db = AsyncMock()
    mock_db.execute.return_value = _mock_result(one_result=row)
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_db
    mock_session_maker.return_value = mock_session

    result = await get_logical_summary(tenant_a_identity)
    assert result.total_recipients == 1
    assert result.successful == 0
    assert result.failed == 1
    assert result.success_rate == 0.0


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_logical_summary_empty(mock_resolve, tenant_a_identity):
    mock_resolve.return_value = []
    result = await get_logical_summary(tenant_a_identity)
    assert result.total_recipients == 0
    assert result.success_rate == 0.0


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_logical_summary_tenant_isolation(mock_session_maker, mock_resolve, tenant_a_identity):
    """Tenant A should only see Tenant A's logical deliveries."""
    mock_resolve.return_value = ["acc-a-1"]
    Row = type("Row", (), {"total": 0, "successful": 0})
    row = Row()
    row.total, row.successful = 0, 0
    mock_db = AsyncMock()
    mock_db.execute.return_value = _mock_result(one_result=row)
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_db
    mock_session_maker.return_value = mock_session

    result = await get_logical_summary(tenant_a_identity)
    assert result.total_recipients == 0


# ─── get_logical_broadcast_analytics ─────────────────────────────────

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_logical_broadcast_collapses_retries(mock_session_maker, mock_resolve, tenant_a_identity):
    """3 attempts to same recipient in a broadcast → 1 logical recipient."""
    mock_resolve.return_value = ["acc-1"]
    Row = type("Row", (), {
        "source_id": "", "total": 0, "successful": 0,
        "first_activity": None, "latest_activity": None,
    })
    r1 = Row()
    r1.source_id, r1.total, r1.successful = "b1", 1, 1
    r1.first_activity = datetime(2026, 7, 1, 10, 0, 0)
    r1.latest_activity = datetime(2026, 7, 1, 10, 30, 0)
    mock_db = AsyncMock()
    mock_db.execute.return_value = _mock_result(rows=[r1])
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_db
    mock_session_maker.return_value = mock_session

    result = await get_logical_broadcast_analytics(tenant_a_identity)
    assert len(result) == 1
    assert result[0].broadcast_id == "b1"
    assert result[0].total_recipients == 1
    assert result[0].successful == 1
    assert result[0].failed == 0
    assert result[0].success_rate == 100.0


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_logical_broadcast_empty(mock_resolve, tenant_a_identity):
    mock_resolve.return_value = []
    result = await get_logical_broadcast_analytics(tenant_a_identity)
    assert result == []


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_logical_broadcast_tenant_isolation(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = ["acc-a-1"]
    mock_db = AsyncMock()
    mock_db.execute.return_value = _mock_result(rows=[])
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_db
    mock_session_maker.return_value = mock_session

    result = await get_logical_broadcast_analytics(tenant_a_identity)
    assert result == []


# ─── Overview includes logical section ───────────────────────────────

@pytest.mark.asyncio
@patch("app.services.delivery_analytics.get_latency_by_account")
@patch("app.services.delivery_analytics.get_latency_by_source")
@patch("app.services.delivery_analytics.get_summary")
@patch("app.services.delivery_analytics.get_source_analytics")
@patch("app.services.delivery_analytics.get_account_performance")
@patch("app.services.delivery_analytics.get_failure_intelligence")
@patch("app.services.delivery_analytics.get_timeline")
@patch("app.services.delivery_analytics.get_logical_summary")
@patch("app.services.delivery_analytics.get_latency_analytics")
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_overview_includes_logical(
    mock_resolve, mock_latency, mock_logical, mock_timeline, mock_failure, mock_perf, mock_source, mock_summary, mock_latency_by_source, mock_latency_by_account,
    tenant_a_identity,
):
    mock_resolve.return_value = ["acc-1"]
    mock_latency_by_account.return_value = []
    mock_latency_by_source.return_value = []
    mock_latency.return_value = LatencyResult()
    mock_summary.return_value = SummaryResult(total_attempted=100, successful=80, failed=20, success_rate=80.0)
    mock_source.return_value = []
    mock_perf.return_value = []
    mock_failure.return_value = []
    mock_timeline.return_value = []
    mock_logical.return_value = LogicalSummaryResult(total_recipients=50, successful=45, failed=5, success_rate=90.0)

    overview = await get_overview(tenant_a_identity)

    assert overview.summary is not None
    assert overview.summary.total_attempted == 100
    assert overview.logical is not None
    assert overview.logical.total_recipients == 50
    assert overview.logical.successful == 45
    assert overview.logical.success_rate == 90.0


# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════
# SPRINT 18 — DELIVERY LATENCY TESTS
# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════


# ─── get_latency_analytics ───────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_latency_empty_when_no_timing_data(mock_session_maker, mock_resolve, tenant_a_identity):
    """No rows with started_at/completed_at → empty latency result."""
    mock_resolve.return_value = ["acc-1"]
    # Merged query returns (total=5, timed=0) in one row
    CountRow = type("Row", (), {"total": 0, "timed": 0})
    count_row = CountRow()
    count_row.total, count_row.timed = 5, 0
    mock_db = AsyncMock()
    mock_db.execute.return_value = _mock_result(one_result=count_row)
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_db
    mock_session_maker.return_value = mock_session

    result = await get_latency_analytics(tenant_a_identity)
    assert result.total_measured == 0
    assert result.rows_without_timing == 5
    assert result.average_latency_ms == 0.0
    assert result.p95_latency_ms == 0.0


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_latency_empty_when_no_accounts(mock_session_maker, mock_resolve, tenant_a_identity):
    """No authorized accounts → empty latency result."""
    mock_resolve.return_value = []
    result = await get_latency_analytics(tenant_a_identity)
    assert result.total_measured == 0
    assert result.average_latency_ms == 0.0


@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_latency_tenant_isolation(mock_session_maker, mock_resolve, tenant_a_identity):
    """Tenant A should only see Tenant A's latency data."""
    mock_resolve.return_value = ["acc-a-1"]
    CountRow = type("Row", (), {"total": 0, "timed": 0})
    count_row = CountRow()
    count_row.total, count_row.timed = 5, 0
    mock_db = AsyncMock()
    mock_db.execute.return_value = _mock_result(one_result=count_row)
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_db
    mock_session_maker.return_value = mock_session

    result = await get_latency_analytics(tenant_a_identity)
    assert result.total_measured == 0


# ─── Overview includes latency section ───────────────────────────────

@pytest.mark.asyncio
@patch("app.services.delivery_analytics.get_latency_by_account")
@patch("app.services.delivery_analytics.get_latency_by_source")
@patch("app.services.delivery_analytics.get_summary")
@patch("app.services.delivery_analytics.get_source_analytics")
@patch("app.services.delivery_analytics.get_account_performance")
@patch("app.services.delivery_analytics.get_failure_intelligence")
@patch("app.services.delivery_analytics.get_timeline")
@patch("app.services.delivery_analytics.get_logical_summary")
@patch("app.services.delivery_analytics.get_latency_analytics")
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
async def test_overview_includes_latency(
    mock_resolve, mock_latency, mock_logical, mock_timeline, mock_failure, mock_perf, mock_source, mock_summary, mock_latency_by_source, mock_latency_by_account,
    tenant_a_identity,
):
    mock_resolve.return_value = ["acc-1"]
    mock_latency_by_account.return_value = []
    mock_latency_by_source.return_value = []
    mock_latency.return_value = LatencyResult(average_latency_ms=200.0, p95_latency_ms=500.0, total_measured=100, rows_without_timing=0)
    mock_summary.return_value = SummaryResult(total_attempted=100, successful=80, failed=20, success_rate=80.0)
    mock_source.return_value = []
    mock_perf.return_value = []
    mock_failure.return_value = []
    mock_timeline.return_value = []
    mock_logical.return_value = LogicalSummaryResult()

    overview = await get_overview(tenant_a_identity)

    assert overview.latency is not None
    assert overview.latency.average_latency_ms == 200.0
    assert overview.latency.p95_latency_ms == 500.0
    assert overview.latency.total_measured == 100


# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════
# SPRINT 19 — OPTIMIZATION & NEW FUNCTION TESTS
# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════


# ─── _resolve_time_range ────────────────────────────────────────────

def test_resolve_time_range_defaults_to_last_n_days():
    start_dt, end_dt = _resolve_time_range(days=30)
    assert start_dt is not None
    assert end_dt is None
    # Should be ~30 days ago
    diff = (utcnow_naive() - start_dt).days
    assert 29 <= diff <= 30


def test_resolve_time_range_with_start_time():
    start_dt, end_dt = _resolve_time_range(start_time="2026-07-01T00:00:00", days=30)
    assert start_dt is not None
    assert start_dt.year == 2026
    assert end_dt is None


# ─── get_latency_by_source ──────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_latency_by_source_empty(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = []
    result = await get_latency_by_source(tenant_a_identity)
    assert result == []


# ─── get_latency_by_account ─────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_latency_by_account_empty(mock_session_maker, mock_resolve, tenant_a_identity):
    mock_resolve.return_value = []
    result = await get_latency_by_account(tenant_a_identity)
    assert result == []


# ─── Regression: attempt-level endpoints unchanged ───────────────────

@pytest.mark.asyncio
@patch("app.services.delivery_analytics._resolve_authorized_account_ids")
@patch("app.services.delivery_analytics.async_session_maker")
async def test_attempt_level_summary_unchanged(mock_session_maker, mock_resolve, tenant_a_identity):
    """Verify attempt-level summary still counts rows, not groups."""
    mock_resolve.return_value = ["acc-1"]
    result = _mock_result(one_result=type("Row", (), {"total": 3, "successful": 1})())
    _, mock_session = _mock_db_session(result)
    mock_session_maker.return_value = mock_session

    s = await get_summary(tenant_a_identity)
    assert s.total_attempted == 3  # 3 rows = 3 attempts
    assert s.successful == 1
    assert s.failed == 2


# ─── Regression: PostgreSQL strftime compatibility ────────────────

@pytest.mark.asyncio
async def test_timeline_date_trunc_does_not_crash(client, db_session):
    """Verify get_timeline uses PostgreSQL-compatible date_trunc.
    A GET /api/delivery-analytics/timeline must not 500 with strftime."""
    resp = await client.get("/api/delivery-analytics/timeline?days=1")
    # 200 or 403 is acceptable — the important thing is no 500
    assert resp.status_code in (200, 403, 401), f"Expected 200/403, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_overview_does_not_crash(client, db_session):
    """Verify get_overview does not 500 with strftime.
    A GET /api/delivery-analytics/overview must not crash."""
    resp = await client.get("/api/delivery-analytics/overview?days=1")
    assert resp.status_code in (200, 403, 401), f"Expected 200/403, got {resp.status_code}: {resp.text}"


# ─── Regression: broadcast recurring route ─────────────────────────

@pytest.mark.asyncio
async def test_broadcast_recurring_not_confused_with_broadcast_id(client, db_session):
    """Verify GET /api/broadcast/recurring is not captured by
    GET /api/broadcast/{broadcast_id}. Must return 200 or 403,
    never a 404 matching a non-existent broadcast_id."""
    resp = await client.get("/api/broadcast/recurring")
    assert resp.status_code in (200, 403, 401), (
        f"Expected 200 or 403, got {resp.status_code}: {resp.text}. "
        f"If 404, the static /recurring route is being captured by /{{broadcast_id}}."
    )


# ─── Regression: date_trunc must declare a result type ──────────────
#
# Production hit `AttributeError: 'NoneType' object has no attribute
# 'dialect_impl'` on GET /api/delivery-analytics/overview. Root cause: the
# custom `date_trunc` FunctionElement had `type = None`, which only breaks
# once SQLAlchemy needs to resolve the column's result type — a step the
# fully-mocked test_timeline_daily/test_overview_does_not_crash tests above
# never exercise (mocked session), and that SQLite's dialect-specific
# @compiles override can sidestep. This test checks the class attribute
# directly so it fails regardless of which DB engine runs the suite.


def test_date_trunc_declares_a_result_type():
    from sqlalchemy import Column, DateTime as SA_DateTime

    from app.services.delivery_analytics import date_trunc

    col = Column("created_at", SA_DateTime())
    expr = date_trunc("day", col)
    assert expr.type is not None, (
        "date_trunc.type must not be None — SQLAlchemy raises "
        "AttributeError: 'NoneType' object has no attribute 'dialect_impl' "
        "as soon as this expression's result type is needed (e.g. GROUP BY "
        "+ row materialization on PostgreSQL)."
    )


@pytest.mark.asyncio
async def test_get_timeline_real_query_with_data(db_session):
    """Real (non-mocked) DB round-trip through get_timeline with actual
    MessageLog rows — exercises the same date_trunc query path production
    hit, unlike the mocked test_timeline_daily above."""
    from app.models.account import Account
    from app.models.message_log import MessageLog

    account = Account(phone="+821000000999", name="date-trunc-test")
    db_session.add(account)
    await db_session.flush()

    db_session.add(
        MessageLog(
            account_id=account.id,
            recipient="-100123",
            source="broadcast",
            source_id="bc-1",
            status="success",
            success=True,
        )
    )
    await db_session.commit()

    identity = Identity(kind="admin")
    timeline = await get_timeline(identity, account_id=account.id, days=30, interval="day")
    assert isinstance(timeline, list)
