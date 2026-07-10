"""Sprint 21+: Behavioral tests for Account Health Monitoring (upgraded)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from app.api.deps import Identity
from app.models.account import Account
from app.models.message_log import MessageLog
from app.services.account_health import (
    AccountHealthItem,
    get_account_health,
    get_health_summary,
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
        last_error=None,
        last_error_at=None,
        last_success_at=None,
        health_checked_at=None,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
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


@pytest.fixture
def tenant_a_identity():
    return Identity(kind="user", tenant_id="tenant-A")


@pytest.fixture
def admin_identity():
    return Identity(kind="admin")


@pytest.fixture
def no_tenant_identity():
    return Identity(kind="api_key", tenant_id=None)


@pytest.mark.asyncio
@patch("app.services.account_health.async_session_maker")
async def test_health_healthy_account(mock_session_maker, tenant_a_identity):
    account = _make_account(id="acc-1", session_data="encrypted-data")
    log = _make_log(account_id="acc-1", status="success", success=True)

    mock_db1 = AsyncMock()
    mock_db1.execute.return_value = _mock_result(scalars_result=[account])
    mock_session1 = AsyncMock()
    mock_session1.__aenter__.return_value = mock_db1

    mock_db2 = AsyncMock()
    mock_db2.execute.side_effect = [
        _mock_result(scalars_result=[log]),
        _mock_result(rows=[type("Row", (), {"account_id": "acc-1", "total": 5, "successful": 4})()]),
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


@pytest.mark.asyncio
@patch("app.services.account_health.async_session_maker")
async def test_health_no_tenant_returns_empty(mock_session_maker, no_tenant_identity):
    result = await get_account_health(no_tenant_identity)
    assert result == []


@pytest.mark.asyncio
@patch("app.services.account_health.async_session_maker")
async def test_health_empty_accounts_returns_empty(mock_session_maker, tenant_a_identity):
    mock_db = AsyncMock()
    mock_db.execute.return_value = _mock_result(scalars_result=[])
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_db
    mock_session_maker.return_value = mock_session

    result = await get_account_health(tenant_a_identity)
    assert result == []


@pytest.mark.asyncio
@patch("app.services.account_health.async_session_maker")
async def test_health_summary(mock_session_maker, tenant_a_identity):
    """Health summary produces correct aggregated counts."""
    accounts = [
        _make_account(id="acc-1", session_data="encrypted-data"),
        _make_account(id="acc-2", session_data=None),
        _make_account(id="acc-3", status="banned", session_data="encrypted-data"),
    ]
    log_1 = _make_log(account_id="acc-1", status="success", success=True)

    # Session 1: get_account_health fetches accounts
    mock_db1 = AsyncMock()
    mock_db1.execute.return_value = _mock_result(scalars_result=accounts)
    mock_session1 = AsyncMock()
    mock_session1.__aenter__.return_value = mock_db1

    # Session 2: get_account_health fetches latest logs + counts (3 accounts)
    mock_db2 = AsyncMock()
    # 3 latest log queries (one per account), then counts query
    mock_db2.execute.side_effect = [
        _mock_result(scalars_result=[log_1]),      # acc-1 latest
        _mock_result(scalars_result=[]),             # acc-2 latest (no logs)
        _mock_result(scalars_result=[]),             # acc-3 latest (no logs)
        _mock_result(rows=[type("Row", (), {"account_id": "acc-1", "total": 5, "successful": 4})()]),  # counts
    ]
    mock_session2 = AsyncMock()
    mock_session2.__aenter__.return_value = mock_db2

    # Session 3: get_account_summary
    mock_db3 = AsyncMock()
    mock_db3.execute.return_value = _mock_result(scalars_result=accounts)
    mock_session3 = AsyncMock()
    mock_session3.__aenter__.return_value = mock_db3

    mock_session_maker.side_effect = [mock_session1, mock_session2, mock_session3]

    with patch("app.crud.account.get_account_summary", new=AsyncMock(return_value={
        "total_today_sent": 10, "total_groups": 25,
    })):
        summary = await get_health_summary(tenant_a_identity)
        assert summary.total == 3
        assert summary.banned >= 1
        assert summary.not_configured >= 1
        assert summary.total_today_sent == 10
        assert summary.total_groups == 25
