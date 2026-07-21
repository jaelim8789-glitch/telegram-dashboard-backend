"""USDT 입금 자동 감지 서비스 (Trongrid API).

주기적으로 내 지갑 주소의 USDT(TRC20) 입금을 확인하고,
입금이 확인되면 자동으로 요금제를 활성화하고 API 키를 발급합니다.

Plan prices are derived from the canonical PLAN_CATALOG.
"""

import os
import secrets
from datetime import datetime, timedelta

import httpx

from app.config import settings
from app.core.logging import get_logger
from app.core.plans import (
    PLAN_CATALOG,
    get_plan,
    is_deprecated_plan,
)
from app.core.time import utcnow_naive
from app.core.telegram_identity import parse_tg_identifier
from app.database import async_session_maker
from app.models.tenant import Tenant, PaymentRecord
from app.models.api_key import APIKey
from app.models.referral import ReferralCommission
from app.services.telegram_notify import send_telegram_message
from app.services.usage_tracker import apply_plan_limits

logger = get_logger(__name__)

# ─── Configuration ────────────────────────────────────────────────────

USDT_WALLET_ADDRESS = settings.usdt_wallet_address
USDT_NETWORK = settings.usdt_network
REFERRAL_COMMISSION_RATE = 0.10

# TRC20 USDT contract address on Tron mainnet
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRONGRID_API = "https://api.trongrid.io"


# Plan prices in USDT cents (derived from PLAN_CATALOG)
_PLAN_PRICES_CENTS: dict[str, dict[str, int]] = {}
for pid, pdef in PLAN_CATALOG.items():
    _PLAN_PRICES_CENTS[pid] = {}
    for interval, price in pdef["prices_usdt"].items():
        if price > 0:
            _PLAN_PRICES_CENTS[pid][interval] = int(price * 100)


def _all_price_cents() -> list[tuple[str, str, int]]:
    """Return all (plan, billing, cents) tuples for active canonical plans."""
    result = []
    for pid, intervals in _PLAN_PRICES_CENTS.items():
        for interval, cents in intervals.items():
            if cents > 0:
                result.append((pid, interval, cents))
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
        if not memo:
            logger.warning("usdt_tx_missing_memo", tx_id=tx_id, amount_cents=amount_cents)
            db.add(PaymentRecord(tx_id=tx_id, from_address=from_addr, amount_usdt=amount_cents, status="unmatched", block_timestamp=tx.get("block_timestamp", 0)))
            await db.commit()
            return {"status": "unmatched", "reason": "missing_memo", "amount_cents": amount_cents}

        result = await db.execute(select(Tenant).where(Tenant.payment_ref == memo))
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

        # Referral commission — first real payment only (referral_rewarded guards
        # against a renewal re-triggering this). Record in referral_commissions
        # table; actual payout is manual/offline against that ledger.
        if tenant.referred_by and not tenant.referral_rewarded:
            referrer = await db.get(Tenant, tenant.referred_by)
            if referrer is not None:
                commission_cents = int(amount_cents * REFERRAL_COMMISSION_RATE)
                db.add(ReferralCommission(
                    referrer_id=referrer.id,
                    referred_id=tenant.id,
                    amount_cents=commission_cents,
                    rate=int(REFERRAL_COMMISSION_RATE * 100),
                    status="pending",
                ))
                referrer.referral_earnings = (referrer.referral_earnings or 0) + commission_cents
                tenant.referral_rewarded = True
                logger.info(
                    "referral_commission_credited",
                    referrer_tenant_id=referrer.id,
                    referred_tenant_id=tenant.id,
                    commission_cents=commission_cents,
                )

        raw_key = f"sk-{secrets.token_urlsafe(32)}"
        api_key = APIKey(key=raw_key, name=f"USDT-{plan_name}-auto", is_active=True, tenant_id=tenant.id, purpose="payment_issued")
        db.add(api_key)
        await db.flush()

        # Also set api_key_hash on the User record so the key works with
        # both X-API-Key (api_keys table) and login-with-api-key (users.api_key_hash).
        if tenant.phone and not tenant.phone.startswith("pending-"):
            from app.crud import user as user_crud
            user = await user_crud.get_user_by_phone(db, tenant.phone)
            if user is not None and user.api_key_hash is None:
                from app.core.security import hash_api_key
                user.api_key_hash = hash_api_key(raw_key)
                await db.flush()
                logger.info("usdt_user_api_key_hash_set", tenant_id=tenant.id, user_id=user.id)

        db.add(PaymentRecord(
            tx_id=tx_id, tenant_id=tenant.id, from_address=from_addr,
            amount_usdt=amount_cents, plan=plan_name, billing=billing,
            status="completed", api_key_id=api_key.id,
            block_timestamp=tx.get("block_timestamp", 0),
        ))
        await db.commit()

        logger.info("usdt_payment_auto_processed", tenant_id=tenant.id, plan=plan_name, tx_id=tx_id)
        await notify_payment_activated(tenant.phone, plan_name, tenant.billing_period_end, raw_key)
        return {"status": "activated", "tenant_id": tenant.id, "plan": plan_name, "billing": billing, "api_key": raw_key}


async def notify_payment_activated(tenant_phone: str, plan_name: str, billing_period_end, raw_key: str) -> None:
    """Best-effort push to the paying user, when their tenant identity is a
    Telegram-native `tg_<id>` one (i.e. the purchase was bot-initiated, or the
    web purchase used that identity). Silently does nothing for phone-based
    tenants — there's no known chat id to push to."""
    chat_id = parse_tg_identifier(tenant_phone)
    if chat_id is None:
        return

    plan_def = get_plan(plan_name)
    display_name = plan_def["name"] if plan_def else plan_name
    expires = billing_period_end.strftime("%Y-%m-%d") if billing_period_end else "-"

    text = (
        f"✅ 결제가 확인되었습니다! {display_name} 플랜이 활성화되었습니다 ({expires}까지).\n\n"
        f"API 키:\n```\n{raw_key}\n```\n\n"
        f"⚠️ 이 키는 다시 표시되지 않습니다. 지금 안전한 곳에 저장해주세요."
    )
    await send_telegram_message(chat_id, text, parse_mode="Markdown")


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
