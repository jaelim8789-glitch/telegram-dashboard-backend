"""Sprint 21: Behavioral tests for Account Health Monitoring.

Tests derive health from Account model fields and MessageLog data.
No fake online status, no WebSocket, no external pings.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from app.api.deps import Identity
from app.models.account import Account
from app.models.message_log import MessageLog
from app.services.account_health import (
    AccountHealthItem,
    get_account_health,
)


def _make_account(**kwargs) -> Account:
    defaults = dict(
        id="acc-1",
        phone="+821012345678",
        name="Test Account",
        status="active",
        session_data="encrypted-session-data",
        last_activity=None,
        today_sent=0,
        group_count=0,
        auto_reply_enabled=False,
    )
    defaults.update(kwargs)
    return Account(**defaults)


def _make_log(**kwargs) -> MessageLog:
    defaults = dict(
        id="log-1",
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
def admin_identity():
    return Identity(kind="admin")


@pytest.fixture
def no_tenant_identity():
    return Identity(kind="api_key", tenant_id=None)


# ═══════════════════════════════════════════════════════════════════════
# Health status derivation tests
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@patch("app.services.account_health.async_session_maker")
async def test_health_healthy_account(mock_session_maker, tenant_a_identity):
    """Account with session and recent successful delivery → healthy."""
    account = _make_account(id="acc-1", session_data="encrypted-data")
    log = _make_log(account_id="acc-1", status="success", success=True)

    # First call: accounts query
    mock_db1 = AsyncMock()
    mock_db1.execute.return_value = _mock_result(scalars_result=[account])
    mock_session1 = AsyncMock()
    mock_session1.__aenter__.return_value = mock_db1

    # Second call: latest log + counts
    mock_db2 = AsyncMock()
    # latest log query returns the log
    mock_db2.execute.side_effect = [
        _mock_result(scalars_result=[log]),  # latest log
        _mock_result(rows=[type("Row", (), {"account_id": "acc-1", "total": 5, "successful": 4})()]),  # counts
    ]
    mock_session2 = AsyncMock()
    mock_session2.__aenter__.return_value = mock_db2

    mock_session_maker.side_effect = [mock_session1, mock_session2]

    result = await get_account_health(tenant_a_identity)
    assert len(result) == 1
    assert result[0].status == "healthy"
    assert result[0].has_session is True
    assert result[0].recent_success_count == 4
    assert result[0].recent_failure_count == 1


@pytest.mark.asyncio
@patch("app.services.account_health.async_session_maker")
async def test_health_not_configured(mock_session_maker, tenant_a_identity):
    """Account without session_data → not_configured."""
    account = _make_account(id="acc-1", session_data=None)

    mock_db1 = AsyncMock()
    mock_db1.execute.return_value = _mock_result(scalars_result=[account])
    mock_session1 = AsyncMock()
    mock_session1.__aenter__.return_value = mock_db1

    mock_db2 = AsyncMock()
    mock_db2.execute.return_value = _mock_result(rows=[])
    mock_session2 = AsyncMock()
    mock_session2.__aenter__.return_value = mock_db2

    mock_session_maker.side_effect = [mock_session1, mock_session2]

    result = await get_account_health(tenant_a_identity)
    assert len(result) == 1
    assert result[0].status == "not_configured"
    assert result[0].has_session is False


@pytest.mark.asyncio
@patch("app.services.account_health.async_session_maker")
async def test_health_banned_account(mock_session_maker, tenant_a_identity):
    """Account with status=banned → banned."""
    account = _make_account(id="acc-1", status="banned", session_data="encrypted-data")

    mock_db1 = AsyncMock()
    mock_db1.execute.return_value = _mock_result(scalars_result=[account])
    mock_session1 = AsyncMock()
    mock_session1.__aenter__.return_value = mock_db1

    mock_db2 = AsyncMock()
    mock_db2.execute.return_value = _mock_result(rows=[])
    mock_session2 = AsyncMock()
    mock_session2.__aenter__.return_value = mock_db2

    mock_session_maker.side_effect = [mock_session1, mock_session2]

    result = await get_account_health(tenant_a_identity)
    assert len(result) == 1
    assert result[0].status == "banned"


@pytest.mark.asyncio
@patch("app.services.account_health.async_session_maker")
async def test_health_unauthorized(mock_session_maker, tenant_a_identity):
    """Most recent delivery was session_expired → unauthorized."""
    account = _make_account(id="acc-1", session_data="encrypted-data")
    log = _make_log(account_id="acc-1", status="session_expired", success=False)

    mock_db1 = AsyncMock()
    mock_db1.execute.return_value = _mock_result(scalars_result=[account])
    mock_session1 = AsyncMock()
    mock_session1.__aenter__.return_value = mock_db1

    mock_db2 = AsyncMock()
    mock_db2.execute.side_effect = [
        _mock_result(scalars_result=[log]),
        _mock_result(rows=[]),
    ]
    mock_session2 = AsyncMock()
    mock_session2.__aenter__.return_value = mock_db2

    mock_session_maker.side_effect = [mock_session1, mock_session2]

    result = await get_account_health(tenant_a_identity)
    assert len(result) == 1
    assert result[0].status == "unauthorized"
    assert result[0].last_error_status == "session_expired"


@pytest.mark.asyncio
@patch("app.services.account_health.async_session_maker")
async def test_health_rate_limited(mock_session_maker, tenant_a_identity):
    """Most recent delivery was flood_wait → rate_limited."""
    account = _make_account(id="acc-1", session_data="encrypted-data")
    log = _make_log(account_id="acc-1", status="flood_wait", success=False)

    mock_db1 = AsyncMock()
    mock_db1.execute.return_value = _mock_result(scalars_result=[account])
    mock_session1 = AsyncMock()
    mock_session1.__aenter__.return_value = mock_db1

    mock_db2 = AsyncMock()
    mock_db2.execute.side_effect = [
        _mock_result(scalars_result=[log]),
        _mock_result(rows=[]),
    ]
    mock_session2 = AsyncMock()
    mock_session2.__aenter__.return_value = mock_db2

    mock_session_maker.side_effect = [mock_session1, mock_session2]

    result = await get_account_health(tenant_a_identity)
    assert len(result) == 1
    assert result[0].status == "rate_limited"


@pytest.mark.asyncio
@patch("app.services.account_health.async_session_maker")
async def test_health_unknown_with_session_no_history(mock_session_maker, tenant_a_identity):
    """Account with session but no delivery history → unknown."""
    account = _make_account(id="acc-1", session_data="encrypted-data")

    mock_db1 = AsyncMock()
    mock_db1.execute.return_value = _mock_result(scalars_result=[account])
    mock_session1 = AsyncMock()
    mock_session1.__aenter__.return_value = mock_db1

    mock_db2 = AsyncMock()
    mock_db2.execute.return_value = _mock_result(rows=[])
    mock_session2 = AsyncMock()
    mock_session2.__aenter__.return_value = mock_db2

    mock_session_maker.side_effect = [mock_session1, mock_session2]

    result = await get_account_health(tenant_a_identity)
    assert len(result) == 1
    assert result[0].status == "unknown"
    assert result[0].has_session is True


# ═══════════════════════════════════════════════════════════════════════
# Tenant isolation tests
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@patch("app.services.account_health.async_session_maker")
async def test_health_no_tenant_returns_empty(mock_session_maker, no_tenant_identity):
    """API key without tenant context sees no accounts."""
    result = await get_account_health(no_tenant_identity)
    assert result == []


@pytest.mark.asyncio
@patch("app.services.account_health.async_session_maker")
async def test_health_empty_accounts_returns_empty(mock_session_maker, tenant_a_identity):
    """No accounts in tenant → empty result."""
    mock_db = AsyncMock()
    mock_db.execute.return_value = _mock_result(scalars_result=[])
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_db
    mock_session_maker.return_value = mock_session

    result = await get_account_health(tenant_a_identity)
    assert result == []