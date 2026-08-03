"""
NOWPayments Service  Crypto payment processing service.
Webhook data handling and payment processing.
"""

import hashlib
import hmac
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.core.plans import get_plan
from app.core.time import utcnow_naive
from app.models.nowpayments import NowPaymentsTransaction
from app.models.tenant import Tenant
from app.services.cryptomus import activate_tenant_plan
from app.services.referral import create_commission

logger = get_logger(__name__)


class NOWPaymentsService:
    def __init__(self):
        self.api_key = settings.NOWPAYMENTS_API_KEY
        self.public_key = settings.NOWPAYMENTS_PUBLIC_KEY
        self.ipn_secret = settings.NOWPAYMENTS_IPN_SECRET
        self.base_url = "https://api.nowpayments.io/v1"

    def _callback_url(self) -> str:
        """Public callback URL handed to NOWPayments for IPN delivery.

        Uses api_base_url when it is a real (non-localhost) URL, otherwise falls
        back to frontend_url so a misconfigured deployment still points webhooks
        somewhere reachable instead of silently at localhost.
        """
        base = (settings.api_base_url or "").strip().rstrip("/")
        if base and "localhost" not in base and base.startswith(("http://", "https://")):
            return f"{base}/api/payments/nowpayments/webhook"
        frontend = (settings.frontend_url or "").strip().rstrip("/")
        if frontend and "localhost" not in frontend:
            return f"{frontend}/api/payments/nowpayments/webhook"
        logger.warning(
            "nowpayments_callback_url_localhost",
            detail="api_base_url is localhost; IPN webhooks will not be delivered in production",
        )
        return f"{base or 'http://localhost:8000'}/api/payments/nowpayments/webhook"

    async def create_payment(self, amount: float, currency: str, plan_id: str, tenant_id: str, order_description: str = "TeleMon Subscription"):
        """
        NOWPayments

        Args:
            amount:
            currency:  (: usdt, btc, eth )
            plan_id:  ID
            tenant_id:  ID
            order_description:

        Returns:

        """
        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json"
        }

        payload = {
            "price_amount": amount,
            "price_currency": "usd",
            "pay_currency": currency.lower(),
            "order_id": f"tenant_{tenant_id}_plan_{plan_id}_{int(datetime.now().timestamp())}",
            "order_description": order_description,
            "ipn_callback_url": self._callback_url(),
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.base_url}/payment",
                    headers=headers,
                    json=payload
                )

                if response.status_code not in (200, 201):
                    logger.error(f"NOWPayments API error: {response.status_code} - {response.text}")
                    raise RuntimeError(f"NOWPayments API error: {response.status_code}")

                result = response.json()

                transaction = NowPaymentsTransaction(
                    id=result['payment_id'],
                    payment_id=result['payment_id'],
                    tenant_id=tenant_id,
                    plan_id=plan_id,
                    amount=result['price_amount'],
                    pay_currency=result['pay_currency'],
                    order_id=result['order_id'],
                    payment_status='created',
                    created_at=utcnow_naive()
                )
                from app.database import async_session_maker
                async with async_session_maker() as db:
                    db.add(transaction)
                    await db.commit()

                return result
        except Exception as e:
            logger.error(f"Error creating NOWPayments invoice: {str(e)}")
            raise

    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """
        IPN

        Args:
            payload:
            signature:

        Returns:

        """
        if not self.ipn_secret:
            logger.error("NOWPAYMENTS_IPN_SECRET not configured")
            return False

        # HMAC-SHA512
        computed_signature = hmac.new(
            self.ipn_secret.encode('utf-8'),
            payload,
            hashlib.sha512
        ).hexdigest()

        #
        return hmac.compare_digest(computed_signature, signature)

    async def get_payment_status(self, payment_id: str) -> Optional[Dict[str, Any]]:
        """Server-side re-verification via the NOWPayments API.

        NOWPayments recommends cross-checking the webhook body against their
        API so a payment can't be fulfilled based on a forged/relayed webhook
        alone. Returns the raw API status dict, or None on failure.
        """
        if not self.api_key:
            logger.error("NOWPAYMENTS_API_KEY not configured; cannot re-verify")
            return None
        headers = {"x-api-key": self.api_key}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{self.base_url}/payment/{payment_id}",
                    headers=headers,
                )
                if response.status_code != 200:
                    logger.warning(
                        "nowpayments_verify_failed",
                        payment_id=payment_id,
                        status_code=response.status_code,
                    )
                    return None
                return response.json()
        except Exception as e:
            logger.warning("nowpayments_verify_error", payment_id=payment_id, error=str(e))
            return None

    def _parse_order_id(self, order_id: str) -> tuple[Optional[str], Optional[str]]:
        """Robustly extract (tenant_id, plan_id) from our order_id.

        Format is ``tenant_<tenant_id>_plan_<plan_id>_<ts>``. tenant_id itself may
        contain underscores, so we anchor on the literal ``_plan_`` separator
        instead of positional splitting.
        """
        if not order_id or not order_id.startswith("tenant_"):
            return None, None
        marker = "_plan_"
        idx = order_id.find(marker)
        if idx <= 0:
            return None, None
        tenant_id = order_id[len("tenant_"):idx]
        rest = order_id[idx + len(marker):]
        # rest = "<plan_id>_<ts>" (ts itself may contain no underscores; keep it simple)
        plan_id = rest.split("_")[0] if rest else None
        return tenant_id or None, plan_id or None

    async def process_webhook(self, webhook_data: Dict[str, Any], db: AsyncSession):
        """
        NOWPayments IPN webhook processing with:
          - idempotent fulfillment (fulfilled marker, not just status)
          - server-side re-verification before fulfilling
          - currency-aware amount validation
          - explicit handling of failed/expired/refunded states

        Args:
            webhook_data:
            db:
        """
        payment_id = webhook_data.get('payment_id')
        status = webhook_data.get('payment_status')
        paid_amount = float(webhook_data.get('paid_amount', 0) or 0)
        pay_currency = webhook_data.get('pay_currency', '').lower()
        order_id = webhook_data.get('order_id')

        logger.info(f"Processing NOWPayments webhook for payment_id: {payment_id}, status: {status}")

        if not payment_id:
            logger.error("nowpayments_webhook_no_payment_id")
            return

        # Look up by payment_id; prefer stored tenant/plan over parsing order_id
        existing_transaction = await db.execute(
            select(NowPaymentsTransaction).where(NowPaymentsTransaction.payment_id == payment_id)
        )
        transaction = existing_transaction.scalar_one_or_none()

        if not transaction:
            logger.error(f"No existing transaction found for payment_id: {payment_id}")
            return

        tenant_id = transaction.tenant_id
        plan_id = transaction.plan_id
        if not tenant_id or not plan_id:
            # fall back to order_id parsing for legacy rows
            parsed_tenant, parsed_plan = self._parse_order_id(order_id)
            tenant_id = tenant_id or parsed_tenant
            plan_id = plan_id or parsed_plan

        # ── Idempotency: a payment is fulfilled exactly once ──────────────
        # fulfilled flag (not just payment_status) guards against a concurrent
        # duplicate webhook racing past the status check before the first
        # handler commits — both would otherwise issue a fresh API key.
        if transaction.fulfilled:
            logger.info(f"Payment {payment_id} already fulfilled; ignoring duplicate")
            return

        # Persist the latest status for observability.
        transaction.payment_status = status
        transaction.paid_amount = paid_amount
        transaction.pay_currency = pay_currency or transaction.pay_currency

        if status in ['finished', 'confirmed']:
            if not await self._fulfill_transaction(db, transaction, tenant_id, plan_id, paid_amount, pay_currency):
                await db.commit()
                return
        elif status in ['failed', 'expired', 'refunded', 'partially_paid']:
            transaction.note = f"Payment {status}. Awaiting manual review."
            logger.warning(f"Payment {payment_id} marked {status} by webhook")
        else:
            logger.info(f"Payment {payment_id} status {status} — no action")

        await db.commit()

    async def _fulfill_transaction(
        self,
        db: AsyncSession,
        transaction: NowPaymentsTransaction,
        tenant_id: Optional[str],
        plan_id: Optional[str],
        paid_amount: float,
        pay_currency: str,
    ) -> bool:
        """Fulfill a finished/confirmed payment.

        Server-side re-verifies the payment with NOWPayments, validates the paid
        amount in the correct currency, marks the transaction fulfilled exactly
        once, then activates the plan and records the commission.

        Returns True when fulfilled, False when the payment must not be
        fulfilled (mismatch / verify failed / invalid plan / missing tenant).
        """
        payment_id = transaction.payment_id

        # 1) Server-side re-verification — don't trust the webhook body alone.
        api_status = await self.get_payment_status(payment_id)
        if api_status is None:
            transaction.note = "Server-side re-verification failed; payment not fulfilled."
            logger.error(f"nowpayments_verify_unavailable", payment_id=payment_id)
            return False
        api_state = (api_status.get('payment_status') or '').lower()
        if api_state not in ('finished', 'confirmed'):
            transaction.note = (
                f"Re-verification status is '{api_state}', not finished/confirmed. "
                f"Payment not fulfilled."
            )
            logger.warning(f"nowpayments_verify_status_mismatch", payment_id=payment_id, api_state=api_state)
            return False
        # Prefer the API's authoritative amount (it reports the pay-currency amount paid).
        verified_paid = api_status.get('actually_paid') or api_status.get('pay_amount')
        if verified_paid is not None:
            try:
                paid_amount = float(verified_paid)
            except (TypeError, ValueError):
                pass

        # 2) Currency-aware amount validation.
        plan = get_plan(plan_id) if plan_id else None
        if not plan:
            transaction.note = f"Invalid plan_id: {plan_id}"
            logger.error(f"Invalid plan_id: {plan_id}")
            return False

        billing = "quarterly" if "quarterly" in plan["prices_usdt"] else "monthly"
        expected_usd = plan["prices_usdt"].get(billing, 0)

        # paid_amount is in pay_currency; expected is USD. For USDT/USDC (1:1
        # with USD) a direct comparison is valid. For other currencies we fetch
        # the exchange rate so a BTC/ETH/etc payment can actually fulfill.
        if pay_currency in ('usdt', 'usdttrc20', 'usdtton', 'usdc', 'usd'):
            paid_usd = paid_amount
        else:
            rate = await self._get_usd_rate(pay_currency)
            if rate is None:
                transaction.note = f"Could not fetch exchange rate for {pay_currency}; not fulfilled."
                logger.error(f"nowpayments_rate_unavailable", currency=pay_currency, payment_id=payment_id)
                return False
            paid_usd = Decimal(str(paid_amount)) * Decimal(str(rate))

        tolerance = Decimal("0.01")
        if abs(Decimal(str(paid_usd)) - Decimal(str(expected_usd))) > tolerance:
            transaction.note = (
                f"Amount mismatch. Expected(USD): {expected_usd}, "
                f"Paid({pay_currency}): {paid_amount} -> USD: {float(paid_usd):.2f}"
            )
            logger.error(
                f"Amount mismatch for payment {payment_id}. Expected: {expected_usd}, Paid: {paid_usd}"
            )
            # Do NOT mark fulfilled — a later webhook with the corrected amount
            # must still be able to fulfill this payment.
            return False

        # 3) Load tenant and fulfill exactly once.
        tenant = await db.get(Tenant, tenant_id) if tenant_id else None
        if tenant is None:
            transaction.note = f"Tenant not found: {tenant_id}"
            logger.error(f"Tenant not found: {tenant_id}")
            return False

        # 4) Atomic claim — the ONLY thing that decides who fulfills this
        #    payment. Two concurrent webhooks may both pass the ORM-level
        #    `fulfilled` check above (both read False before either commits);
        #    this conditional UPDATE serializes them: only one row is flipped
        #    to fulfilled=true, the other gets rowcount 0 and bails.
        claimed = await db.execute(
            update(NowPaymentsTransaction)
            .where(NowPaymentsTransaction.payment_id == payment_id)
            .where(NowPaymentsTransaction.fulfilled.is_(False))
            .values(fulfilled=True, fulfilled_at=utcnow_naive())
        )
        if claimed.rowcount != 1:
            transaction.note = "Concurrent webhook already claimed this payment; skipped."
            logger.warning(f"nowpayments_claim_lost", payment_id=payment_id)
            return False

        result = await activate_tenant_plan(db, tenant_id, plan_id)
        if not result.get("success"):
            # Release the claim so a later webhook / reconciliation can retry.
            await db.execute(
                update(NowPaymentsTransaction)
                .where(NowPaymentsTransaction.payment_id == payment_id)
                .values(fulfilled=False, fulfilled_at=None)
            )
            transaction.note = f"Plan activation failed: {result.get('error', 'unknown')}"
            logger.error(f"nowpayments_activation_failed", tenant_id=tenant_id, error=result.get('error'))
            return False

        amount_cents = int(Decimal(str(paid_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)
        await create_commission(db, tenant_id, payment_id, "nowpayments", amount_cents)

        transaction.fulfilled = True
        transaction.fulfilled_at = utcnow_naive()
        transaction.note = result.get("api_key") and "API key issued" or transaction.note

        logger.info(f"Successfully processed payment {payment_id} for tenant {tenant_id}, plan {plan_id}")
        return True

    async def _get_usd_rate(self, currency: str) -> Optional[Decimal]:
        """Fetch NOWPayments USD exchange rate for a pay currency."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{self.base_url}/rate",
                    params={"currency_from": currency, "currency_to": "usd"},
                )
                if response.status_code != 200:
                    logger.warning("nowpayments_rate_error", currency=currency, status_code=response.status_code)
                    return None
                data = response.json()
                rate = data.get('rate') or data.get('rate_usd')
                if rate is None:
                    return None
                return Decimal(str(rate))
        except Exception as e:
            logger.warning("nowpayments_rate_exception", currency=currency, error=str(e))
            return None

    async def reconcile_pending(self) -> dict[str, int]:
        """Scheduled reconciliation for NOWPayments payments.

        The IPN webhook is the primary delivery mechanism, but a missed webhook
        would otherwise leave a paid invoice unfulfilled forever (the USDT path
        has the same guard with check_usdt_payments). Every non-fulfilled
        transaction created more than 10 minutes ago is re-verified against the
        NOWPayments API; any that are now finished/confirmed are fulfilled.

        Returns a summary dict for logging: {"checked": N, "fulfilled": M}.
        """
        cutoff = utcnow_naive()
        from datetime import timedelta

        from app.database import async_session_maker

        checked = 0
        fulfilled = 0
        try:
            async with async_session_maker() as db:
                result = await db.execute(
                    select(NowPaymentsTransaction).where(
                        NowPaymentsTransaction.fulfilled.is_(False),
                        NowPaymentsTransaction.created_at < cutoff - timedelta(minutes=10),
                    )
                )
                pending = result.scalars().all()
                for txn in pending:
                    checked += 1
                    # Re-fetch authoritative status from NOWPayments.
                    api_status = await self.get_payment_status(txn.payment_id)
                    if api_status is None:
                        continue
                    state = (api_status.get('payment_status') or '').lower()
                    if state in ('finished', 'confirmed'):
                        paid = api_status.get('actually_paid') or api_status.get('pay_amount')
                        paid_amount = float(paid) if paid is not None else 0.0
                        currency = (api_status.get('pay_currency') or txn.pay_currency or '').lower()
                        if await self._fulfill_transaction(
                            db, txn, txn.tenant_id, txn.plan_id, paid_amount, currency
                        ):
                            fulfilled += 1
                        # Commit regardless: success persists the fulfilled claim,
                        # failure persists the diagnostic note on the transaction.
                        await db.commit()
                    else:
                        # Record non-terminal state for observability.
                        txn.payment_status = state
                        await db.commit()
        except Exception as e:
            logger.error("nowpayments_reconcile_error", error=str(e))
        logger.info("nowpayments_reconcile_done", checked=checked, fulfilled=fulfilled)
        return {"checked": checked, "fulfilled": fulfilled}


#
_nowpayments_service = None


def get_nowpayments_service() -> NOWPaymentsService:
    global _nowpayments_service
    if _nowpayments_service is None:
        _nowpayments_service = NOWPaymentsService()
    return _nowpayments_service


async def check_nowpayments_payments() -> None:
    """APScheduler entrypoint wrapping reconcile_pending."""
    service = get_nowpayments_service()
    await service.reconcile_pending()