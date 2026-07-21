"""
Tests for Cryptomus crypto payment router.

Covers:
  - create-invoice success and validation
  - webhook signature verification (valid / invalid)
  - duplicate webhook prevention
  - amount mismatch handling
  - payment status retrieval
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import status
from sqlalchemy import select

from app.core.security import generate_user_api_key
from app.models.cryptomus_payment import CryptomusPayment
from app.models.tenant import Tenant
from app.services.usage_tracker import apply_plan_limits


CRYPTOMUS_WEBHOOK_SECRET = os.environ.get("CRYPTOMUS_WEBHOOK_SECRET", "test_secret")


class db_session_cm:
    """Wrap an already-open test db_session as an async-context-manager."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def _make_tenant(db, *, plan="pro", **overrides):
    tenant = Tenant(phone=overrides.pop("phone", f"+8210{os.urandom(4).hex()}"))
    db.add(tenant)
    await db.flush()
    await apply_plan_limits(db, tenant, plan)
    for key, value in overrides.items():
        setattr(tenant, key, value)
    await db.commit()
    await db.refresh(tenant)
    return tenant


@pytest.mark.asyncio
async def test_create_invoice_success(client, db_session, monkeypatch):
    import app.services.cryptomus as cryptomus_module

    monkeypatch.setenv("CRYPTOMUS_WEBHOOK_SECRET", CRYPTOMUS_WEBHOOK_SECRET)
    monkeypatch.setattr(cryptomus_module, "async_session_maker", lambda: db_session_cm(db_session))

    result_data = {
        "uuid": "inv-123",
        "order_id": "ord-123",
        "status": "process",
        "amount": "100",
        "currency": "USDT",
        "network": "TRC20",
        "address": "TTestAddress123",
        "qr_code": "https://example.com/qr.png",
        "expired_at": "2026-07-21T10:30:00Z",
    }
    monkeypatch.setattr(
        "app.api.cryptomus_payments.create_cryptomus_invoice",
        AsyncMock(return_value=result_data),
    )

    resp = await client.post(
        "/api/payments/crypto/create-invoice",
        json={"plan": "pro", "network": "TRC20"},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["ok"] is True
    assert data["invoice_id"] == "inv-123"
    assert data["plan"] == "pro"
    assert data["network"] == "TRC20"
    assert data["amount_usd"] == 100
    assert data["payment_address"] == "TTestAddress123"


@pytest.mark.asyncio
async def test_create_invoice_invalid_plan(client):
    resp = await client.post(
        "/api/payments/crypto/create-invoice",
        json={"plan": "unknown", "network": "TRC20"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio
async def test_create_invoice_free_plan(client):
    resp = await client.post(
        "/api/payments/crypto/create-invoice",
        json={"plan": "free", "network": "TRC20"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "FREE plan does not require payment" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_invoice_invalid_network(client):
    resp = await client.post(
        "/api/payments/crypto/create-invoice",
        json={"plan": "pro", "network": "INVALID"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Invalid network" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_valid_signature_paid(client, db_session, monkeypatch):
    import app.services.cryptomus as cryptomus_module

    monkeypatch.setenv("CRYPTOMUS_WEBHOOK_SECRET", CRYPTOMUS_WEBHOOK_SECRET)
    monkeypatch.setattr(cryptomus_module, "async_session_maker", lambda: db_session_cm(db_session))

    tenant = await _make_tenant(db_session, plan="free")
    invoice_id = "inv-webhook-1"
    payment = CryptomusPayment(
        tenant_id=tenant.id,
        invoice_id=invoice_id,
        order_id="ord-1",
        plan="pro",
        network="TRC20",
        amount_usd=100,
        currency="USDT",
        status="pending",
        created_at=__import__("datetime").datetime.now(),
    )
    db_session.add(payment)
    await db_session.commit()
    await db_session.refresh(payment)

    payload = {
        "uuid": invoice_id,
        "order_id": "ord-1",
        "status": "paid",
        "amount": "100",
        "payment_amount": "100",
        "payment_currency": "USDT",
        "network": "TRC20",
        "address": "TTestAddress123",
    }
    body = json.dumps(payload).encode()
    sign = hmac.new(
        CRYPTOMUS_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    resp = await client.post(
        "/api/payments/crypto/webhook",
        content=body,
        headers={"sign": sign, "Content-Type": "application/json"},
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["ok"] is True

    await db_session.refresh(payment)
    assert payment.status == "paid"
    assert payment.issued_api_key is not None


@pytest.mark.asyncio
async def test_webhook_invalid_signature(client):
    payload = {
        "uuid": "inv-any",
        "order_id": "ord-any",
        "status": "paid",
        "amount": "100",
        "payment_amount": "100",
        "payment_currency": "USDT",
        "network": "TRC20",
        "address": "TTestAddress123",
    }
    body = json.dumps(payload).encode()
    resp = await client.post(
        "/api/payments/crypto/webhook",
        content=body,
        headers={"sign": "bad_sign", "Content-Type": "application/json"},
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_webhook_duplicate_prevention(client, db_session, monkeypatch):
    import app.services.cryptomus as cryptomus_module

    monkeypatch.setenv("CRYPTOMUS_WEBHOOK_SECRET", CRYPTOMUS_WEBHOOK_SECRET)
    monkeypatch.setattr(cryptomus_module, "async_session_maker", lambda: db_session_cm(db_session))

    tenant = await _make_tenant(db_session, plan="free")
    invoice_id = "inv-dup-1"
    payment = CryptomusPayment(
        tenant_id=tenant.id,
        invoice_id=invoice_id,
        order_id="ord-dup",
        plan="pro",
        network="TRC20",
        amount_usd=100,
        currency="USDT",
        status="paid",
        created_at=__import__("datetime").datetime.now(),
    )
    db_session.add(payment)
    await db_session.commit()
    await db_session.refresh(payment)

    payload = {
        "uuid": invoice_id,
        "order_id": "ord-dup",
        "status": "paid",
        "amount": "100",
        "payment_amount": "100",
        "payment_currency": "USDT",
        "network": "TRC20",
        "address": "TTestAddress123",
    }
    body = json.dumps(payload).encode()
    sign = hmac.new(
        CRYPTOMUS_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    resp = await client.post(
        "/api/payments/crypto/webhook",
        content=body,
        headers={"sign": sign, "Content-Type": "application/json"},
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["ok"] is True
    assert resp.json().get("duplicate") is True


@pytest.mark.asyncio
async def test_webhook_amount_mismatch(client, db_session, monkeypatch):
    import app.services.cryptomus as cryptomus_module

    monkeypatch.setenv("CRYPTOMUS_WEBHOOK_SECRET", CRYPTOMUS_WEBHOOK_SECRET)
    monkeypatch.setattr(cryptomus_module, "async_session_maker", lambda: db_session_cm(db_session))

    tenant = await _make_tenant(db_session, plan="free")
    invoice_id = "inv-mismatch-1"
    payment = CryptomusPayment(
        tenant_id=tenant.id,
        invoice_id=invoice_id,
        order_id="ord-mm",
        plan="pro",
        network="TRC20",
        amount_usd=100,
        currency="USDT",
        status="pending",
        created_at=__import__("datetime").datetime.now(),
    )
    db_session.add(payment)
    await db_session.commit()
    await db_session.refresh(payment)

    payload = {
        "uuid": invoice_id,
        "order_id": "ord-mm",
        "status": "paid",
        "amount": "100",
        "payment_amount": "199.99",
        "payment_currency": "USDT",
        "network": "TRC20",
        "address": "TTestAddress123",
    }
    body = json.dumps(payload).encode()
    sign = hmac.new(
        CRYPTOMUS_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    resp = await client.post(
        "/api/payments/crypto/webhook",
        content=body,
        headers={"sign": sign, "Content-Type": "application/json"},
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["ok"] is True
    assert resp.json().get("error") == "amount_mismatch"

    await db_session.refresh(payment)
    assert payment.status == "amount_mismatch"


@pytest.mark.asyncio
async def test_webhook_failed_status(client, db_session, monkeypatch):
    import app.services.cryptomus as cryptomus_module

    monkeypatch.setenv("CRYPTOMUS_WEBHOOK_SECRET", CRYPTOMUS_WEBHOOK_SECRET)
    monkeypatch.setattr(cryptomus_module, "async_session_maker", lambda: db_session_cm(db_session))

    tenant = await _make_tenant(db_session, plan="free")
    invoice_id = "inv-fail-1"
    payment = CryptomusPayment(
        tenant_id=tenant.id,
        invoice_id=invoice_id,
        order_id="ord-fail",
        plan="pro",
        network="TRC20",
        amount_usd=100,
        currency="USDT",
        status="pending",
        created_at=__import__("datetime").datetime.now(),
    )
    db_session.add(payment)
    await db_session.commit()
    await db_session.refresh(payment)

    payload = {
        "uuid": invoice_id,
        "order_id": "ord-fail",
        "status": "failed",
        "amount": "100",
        "payment_amount": "0",
        "payment_currency": "",
        "network": "TRC20",
        "address": "TTestAddress123",
    }
    body = json.dumps(payload).encode()
    sign = hmac.new(
        CRYPTOMUS_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    resp = await client.post(
        "/api/payments/crypto/webhook",
        content=body,
        headers={"sign": sign, "Content-Type": "application/json"},
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["ok"] is True

    await db_session.refresh(payment)
    assert payment.status == "failed"


@pytest.mark.asyncio
async def test_payment_status_success(client, db_session, monkeypatch):
    import app.services.cryptomus as cryptomus_module

    monkeypatch.setattr(cryptomus_module, "async_session_maker", lambda: db_session_cm(db_session))

    tenant = await _make_tenant(db_session, plan="free")
    invoice_id = "inv-status-1"
    raw_key = generate_user_api_key()
    payment = CryptomusPayment(
        tenant_id=tenant.id,
        invoice_id=invoice_id,
        order_id="ord-status",
        plan="pro",
        network="TRC20",
        amount_usd=100,
        currency="USDT",
        status="paid",
        payment_address="TTestAddress123",
        qr_code_url="https://example.com/qr.png",
        expires_at="2026-07-21T10:30:00Z",
        paid_amount="100",
        paid_currency="USDT",
        issued_api_key=raw_key,
        created_at=__import__("datetime").datetime.now(),
    )
    db_session.add(payment)
    await db_session.commit()
    await db_session.refresh(payment)

    resp = await client.get(f"/api/payments/crypto/status/{invoice_id}")
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["ok"] is True
    assert data["status"] == "paid"
    assert data["api_key"] == raw_key

