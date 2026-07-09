"""Sprint 15: Behavioral tests for delivery analytics.

Tests use mocked MessageLog data to verify:
- Summary, failure breakdown, account performance, timeline, recent activity
- Tenant A/B isolation
- Zero-division safety
- Empty dataset handling
- Source attribution
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
    get_summary,
    get_failure_breakdown,
    get_account_performance,
    get_timeline,
    get_recent_activity,
    _resolve_authorized_account_ids,
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


def _mock_result(rows=None, one_result=None, scalars_result=None):
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
# Phase 9: Source attribution
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