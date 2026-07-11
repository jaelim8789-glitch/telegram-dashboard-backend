"""USDT 입금 자동 감지 서비스 (Trongrid API).

주기적으로 내 지갑 주소의 USDT(TRC20) 입금을 확인하고,
입금이 확인되면 자동으로 요금제를 활성화하고 API 키를 발급합니다.

Plan prices are derived from the canonical PLAN_CATALOG.
"""

import os
import secrets
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings
from app.core.logging import get_logger
from app.core.plans import (
    PLAN_CATALOG,
    get_plan_price_usdt,
    is_deprecated_plan,
)
from app.database import async_session_maker
from app.models.tenant import Tenant, PaymentRecord
from app.models.api_key import APIKey
from app.services.usage_tracker import apply_plan_limits

logger = get_logger(__name__)

# ─── Configuration ────────────────────────────────────────────────────

USDT_WALLET_ADDRESS = settings.usdt_wallet_address
USDT_NETWORK = settings.usdt_network

# TRC20 USDT contract address on Tron mainnet
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRONGRID_API = "https://api.trongrid.io"


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Plan prices in USDT cents (derived from PLAN_CATALOG)
_PLAN_PRICES_CENTS: dict[str, dict[str, int]] = {}
for pid, pdef in PLAN_CATALOG.items():
    _PLAN_PRICES_CENTS[pid] = {}
    for interval, price in pdef["prices_usdt"].items():
        _PLAN_PRICES_CENTS[pid][interval] = int(price * 100)


# Legacy plan prices (for backward compatibility with existing pending tenants)
_LEGACY_PRICES_CENTS = {
    "basic": 1500,
    "enterprise": 15000,
}


def _all_price_cents() -> list[tuple[str, str, int]]:
    """Return all (plan, billing, cents) tuples including legacy."""
    result = []
    for pid, intervals in _PLAN_PRICES_CENTS.items():
        for interval, cents in intervals.items():
            if cents > 0:
                result.append((pid, interval, cents))
    for pid, cents in _LEGACY_PRICES_CENTS.items():
        result.append((pid, "monthly", cents))
    return result


# ─── Trongrid API ────────────────────────────────────────────────────


async def get_usdt_transactions(since_timestamp: int | None = None) -> list[dict]:
    """Fetch USDT(TRC20) transactions for our wallet from Trongrid."""
    if not USDT_WALLET_ADDRESS:
        logger.warning("USDT_WALLET_ADDRESS not configured")
        return []

    url = f"{TRONGRID_API}/v1/accounts/{USDT_WALLET_ADDRESS}/transactions/trc20"
    params = {
        "contract_address": USDT_CONTRACT,
        "limit": 50,
        "order_by": "block_timestamp,desc",
        "only_to": "true",
    }
    if since_timestamp:
        params["min_timestamp"] = since_timestamp

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        txs = []
        for tx in data.get("data", []):
            value_str = tx.get("value", "0")
            try:
                amount_decimal = int(value_str) / 1_000_000
            except (ValueError, TypeError):
                continue

            token_info = tx.get("token_info", {})
            if token_info.get("symbol", "").upper() != "USDT":
                continue

            txs.append({
                "tx_id": tx.get("transaction_id", ""),
                "from_address": tx.get("from", ""),
                "amount_usdt": round(amount_decimal, 2),
                "amount_cents": round(amount_decimal * 100),
                "block_timestamp": tx.get("block_timestamp", 0),
                "memo": tx.get("data", "") or "",
            })
        return txs

    except httpx.HTTPError as exc:
        logger.error("trongrid_api_error", error=str(exc))
        return []


def match_plan(amount_cents: int):
    """Match payment amount to a plan. Returns (plan_name, billing) or None."""
    candidates = _all_price_cents()
    matches = []
    for plan, billing, price in candidates:
        if price == 0:
            continue
        tol = int(price * 0.1)
        if abs(amount_cents - price) <= tol:
            matches.append((plan, billing, price))
    if matches:
        matches.sort(key=lambda m: abs(amount_cents - m[2]))
        return (matches[0][0], matches[0][1])
    return None


# ─── Payment Processing ──────────────────────────────────────────────


async def process_incoming_tx(tx: dict) -> dict:
    """Process an incoming USDT transaction.

    1. Check if already processed
    2. Find pending tenant by memo or amount
    3. Activate plan + issue API key
    """
    tx_id = tx["tx_id"]
    from_addr = tx["from_address"]
    amount_cents = tx["amount_cents"]
    memo = tx.get("memo", "")

    logger.info("usdt_tx_detected", tx_id=tx_id, from_address=from_addr, amount_cents=amount_cents)

    async with async_session_maker() as db:
        from sqlalchemy import select

        existing = await db.execute(select(PaymentRecord).where(PaymentRecord.tx_id == tx_id))
        if existing.scalar_one_or_none():
            return {"status": "already_processed"}

        tenant = None
        if memo:
            result = await db.execute(select(Tenant).where(Tenant.payment_ref == memo))
            tenant = result.scalar_one_or_none()

        if not tenant:
            pm = match_plan(amount_cents)
            if pm:
                plan_name, billing = pm
                result = await db.execute(
                    select(Tenant).where(
                        Tenant.subscription_status == "pending",
                        Tenant.plan == plan_name,
                    ).order_by(Tenant.created_at.asc()).limit(1)
                )
                tenant = result.scalar_one_or_none()

        if not tenant:
            logger.warning("usdt_tx_unmatched", tx_id=tx_id, amount_cents=amount_cents)
            db.add(PaymentRecord(tx_id=tx_id, from_address=from_addr, amount_usdt=amount_cents, status="unmatched", block_timestamp=tx.get("block_timestamp", 0)))
            await db.commit()
            return {"status": "unmatched", "amount_cents": amount_cents}

        pm = match_plan(amount_cents)
        if not pm:
            return {"status": "amount_mismatch"}
        plan_name, billing = pm

        if is_deprecated_plan(plan_name):
            logger.warning("usdt_tx_deprecated_plan", tx_id=tx_id, plan=plan_name)
            db.add(PaymentRecord(tx_id=tx_id, tenant_id=tenant.id, from_address=from_addr, amount_usdt=amount_cents, plan=plan_name, status="failed", block_timestamp=tx.get("block_timestamp", 0)))
            await db.commit()
            return {"status": "deprecated_plan", "plan": plan_name}

        tenant.subscription_status = "active"
        tenant.trial_expires_at = None
        tenant.billing_period_start = utcnow_naive()
        days = 90 if billing == "quarterly" else 30
        tenant.billing_period_end = utcnow_naive() + timedelta(days=days)
        await apply_plan_limits(db, tenant, plan_name)

        raw_key = f"sk-{secrets.token_urlsafe(32)}"
        api_key = APIKey(key=raw_key, name=f"USDT-{plan_name}-auto", is_active=True, tenant_id=tenant.id)
        db.add(api_key)
        await db.flush()

        db.add(PaymentRecord(
            tx_id=tx_id, tenant_id=tenant.id, from_address=from_addr,
            amount_usdt=amount_cents, plan=plan_name, billing=billing,
            status="completed", api_key_id=api_key.id,
            block_timestamp=tx.get("block_timestamp", 0),
        ))
        await db.commit()

        logger.info("usdt_payment_auto_processed", tenant_id=tenant.id, plan=plan_name, tx_id=tx_id)
        return {"status": "activated", "tenant_id": tenant.id, "plan": plan_name, "billing": billing, "api_key": raw_key}


# ─── Scheduled Checker ──────────────────────────────────────────────


async def check_usdt_payments() -> dict:
    """Scheduled task: check for new USDT payments (every 5 minutes)."""
    from sqlalchemy import select, func

    async with async_session_maker() as db:
        result = await db.execute(select(func.max(PaymentRecord.block_timestamp)))
        last_ts = result.scalar_one_or_none() or 0

    txs = await get_usdt_transactions(since_timestamp=last_ts if last_ts else None)
    results = [await process_incoming_tx(tx) for tx in txs]
    activated = sum(1 for r in results if r.get("status") == "activated")
    unmatched = sum(1 for r in results if r.get("status") == "unmatched")

    if activated or unmatched:
        logger.info("usdt_check_completed", checked=len(txs), activated=activated, unmatched=unmatched)

    return {"checked": len(txs), "activated": activated, "unmatched": unmatched, "results": results}
