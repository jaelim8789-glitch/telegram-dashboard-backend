"""WebSocket tenant-isolation tests.

/ws/chat and /ws/dashboard accept an account_id query param. Without an
ownership check an authenticated user could subscribe to another tenant's
account and receive its live messages/stats (IDOR). These tests verify
_verify_account_access:
  - allows the owning tenant (account.tenant_id == identity.tenant_id)
  - allows admins
  - denies cross-tenant subscriptions (closes 4401)
  - denies tenant-less users
  - denies unknown accounts
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.api.deps import Identity
from app.routes.ws import _verify_account_access


def _fake_ws():
    return AsyncMock()


def _account_row(tenant_id: str | None):
    account = MagicMock()
    account.tenant_id = tenant_id
    return account


def _patch_account_crud(monkeypatch, account):
    fake_crud = MagicMock()
    fake_crud.get_account = AsyncMock(return_value=account)
    monkeypatch.setattr("app.crud.account", fake_crud)
    return fake_crud


@pytest.mark.asyncio
async def test_allows_owning_tenant(monkeypatch):
    identity = Identity(kind="user", tenant_id="tenant-a")
    _patch_account_crud(monkeypatch, _account_row("tenant-a"))
    ws = _fake_ws()

    result = await _verify_account_access(ws, identity, "acc-1")
    assert result is True


@pytest.mark.asyncio
async def test_allows_admin(monkeypatch):
    identity = Identity(kind="admin")
    ws = _fake_ws()

    # Admin short-circuits before any DB access.
    result = await _verify_account_access(ws, identity, "acc-1")
    assert result is True
    ws.accept.assert_not_called()


@pytest.mark.asyncio
async def test_denies_cross_tenant(monkeypatch):
    identity = Identity(kind="user", tenant_id="tenant-a")
    _patch_account_crud(monkeypatch, _account_row("tenant-b"))
    ws = _fake_ws()

    result = await _verify_account_access(ws, identity, "acc-1")
    assert result is False
    ws.accept.assert_awaited_once()
    ws.close.assert_awaited_once_with(code=4401)


@pytest.mark.asyncio
async def test_denies_unknown_account(monkeypatch):
    identity = Identity(kind="user", tenant_id="tenant-a")
    _patch_account_crud(monkeypatch, None)
    ws = _fake_ws()

    result = await _verify_account_access(ws, identity, "ghost-account")
    assert result is False
    ws.close.assert_awaited_once_with(code=4401)


@pytest.mark.asyncio
async def test_denies_tenantless_user(monkeypatch):
    identity = Identity(kind="user", tenant_id=None)
    ws = _fake_ws()

    result = await _verify_account_access(ws, identity, "acc-1")
    assert result is False
    ws.close.assert_awaited_once_with(code=4401)


@pytest.mark.asyncio
async def test_denies_missing_account_id(monkeypatch):
    identity = Identity(kind="user", tenant_id="tenant-a")
    ws = _fake_ws()

    result = await _verify_account_access(ws, identity, None)
    assert result is False
    ws.close.assert_awaited_once_with(code=4401)
