"""Bot-facing account/billing self-service — the business logic behind the
Telegram ops menu (plan info, account status, USDT purchase/renew, purchase
history, payment claim). Mirrors how app.services.bot_api_key_service is
organized: all DB/business logic lives here, telegram_bot_service.py only
formats replies and wires callbacks.

Unlike bot_api_key_service (a recovery-only path for an already-eligible
account), the purchase flow here is intentionally allowed to originate a new
Tenant/User for a Telegram identity that has never touched TeleMon before —
this matches the existing web policy (app/api/usdt_payment.py already permits
an anonymous paid-purchase intent). Free-trial eligibility (channel-gated)
stays a completely separate, untouched flow.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.core.plans import PLAN_CATALOG, get_plan, validate_plan_id
from app.core.rate_limiter import check_rate_limit
from app.core.telegram_identity import tg_identifier
from app.crud import user as user_crud
from app.models.api_key import APIKey
from app.models.tenant import PaymentRecord, Tenant
from app.services import purchase_service

logger = get_logger(__name__)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── Result types ──────────────────────────────────────────────────────


@dataclass
class AccountSnapshot:
    linked: bool
    plan: str | None = None
    plan_name: str | None = None
    subscription_status: str | None = None
    trial_expires_at: datetime | None = None
    billing_period_end: datetime | None = None
    has_api_key: bool = False
    max_accounts: int | None = None
    monthly_message_limit: int | None = None


@dataclass
class BotPurchaseResult:
    status: str  # "ok" | "invalid_plan" | "already_active" | "rate_limited" | "no_prior_plan"
    plan: str | None = None
    plan_name: str | None = None
    billing: str | None = None
    amount_usdt: float | None = None
    wallet_address: str | None = None
    payment_ref: str | None = None
    detail: str = ""


@dataclass
class ClaimResult:
    status: str  # "no_tenant" | "pending" | "no_payment" | "claimed"
    api_key: str | None = None
    detail: str = ""


@dataclass
class CheckinResult:
    status: str  # "no_tenant" | "already_checked_in" | "ok"
    streak: int = 0
    stars_earned: int = 0
    stars_balance: int = 0
    detail: str = ""


CHECKIN_BASE_STARS = 5
CHECKIN_STREAK_MILESTONE_DAYS = 5
CHECKIN_STREAK_MILESTONE_BONUS = 20


# ─── DB helpers ────────────────────────────────────────────────────────


async def _resolve_tenant(db: AsyncSession, telegram_user_id: int) -> Tenant | None:
    identifier = tg_identifier(telegram_user_id)
    result = await db.execute(select(Tenant).where(Tenant.phone == identifier).limit(1))
    return result.scalar_one_or_none()


def _billing_interval_for(plan: str) -> str:
    plan_def = get_plan(plan)
    prices = plan_def["prices_usdt"] if plan_def else {}
    return "monthly" if "monthly" in prices else "quarterly"


# ─── Account snapshot (shared by "내 플랜/만료일" and "계정 상태") ───────


async def get_account_snapshot(db: AsyncSession, telegram_user_id: int) -> AccountSnapshot:
    tenant = await _resolve_tenant(db, telegram_user_id)
    if tenant is None:
        return AccountSnapshot(linked=False)

    identifier = tg_identifier(telegram_user_id)
    user = await user_crud.get_user_by_phone(db, identifier)
    plan_def = get_plan(tenant.plan)

    return AccountSnapshot(
        linked=True,
        plan=tenant.plan,
        plan_name=plan_def["name"] if plan_def else tenant.plan,
        subscription_status=tenant.subscription_status,
        trial_expires_at=tenant.trial_expires_at,
        billing_period_end=tenant.billing_period_end,
        has_api_key=user is not None and user.api_key_hash is not None,
        max_accounts=tenant.max_accounts,
        monthly_message_limit=tenant.monthly_message_limit,
    )


# ─── Daily check-in ─────────────────────────────────────────────────────


async def do_checkin(db: AsyncSession, telegram_user_id: int) -> CheckinResult:
    """Once-per-day check-in. Consecutive days build a streak; the streak
    resets to 1 if a day is missed. Stars are credited via the same wallet
    used for template-marketplace purchases (usage_tracker.add_stars_credit)."""
    tenant = await _resolve_tenant(db, telegram_user_id)
    if tenant is None:
        return CheckinResult(status="no_tenant", detail="먼저 무료체험/요금제를 시작해주세요.")

    now = _utcnow_naive()
    if tenant.last_checkin_at is not None and tenant.last_checkin_at.date() == now.date():
        return CheckinResult(
            status="already_checked_in",
            streak=tenant.checkin_streak,
            stars_balance=tenant.stars_balance,
            detail="오늘은 이미 출석체크를 완료했습니다. 내일 다시 와주세요!",
        )

    missed_a_day = (
        tenant.last_checkin_at is None
        or (now.date() - tenant.last_checkin_at.date()).days > 1
    )
    tenant.checkin_streak = 1 if missed_a_day else tenant.checkin_streak + 1
    tenant.last_checkin_at = now

    stars_earned = CHECKIN_BASE_STARS
    if tenant.checkin_streak % CHECKIN_STREAK_MILESTONE_DAYS == 0:
        stars_earned += CHECKIN_STREAK_MILESTONE_BONUS
    tenant.stars_balance = (tenant.stars_balance or 0) + stars_earned

    await db.commit()
    await db.refresh(tenant)

    logger.info(
        "bot_checkin",
        telegram_user_id=telegram_user_id,
        tenant_id=tenant.id,
        streak=tenant.checkin_streak,
        stars_earned=stars_earned,
    )
    return CheckinResult(
        status="ok",
        streak=tenant.checkin_streak,
        stars_earned=stars_earned,
        stars_balance=tenant.stars_balance,
    )


async def get_checkin_leaderboard(db: AsyncSession, limit: int = 10) -> list[tuple[int, int]]:
    """Top streaks, no PII — just (rank, streak_days) pairs."""
    result = await db.execute(
        select(Tenant.checkin_streak)
        .where(Tenant.checkin_streak > 0)
        .order_by(Tenant.checkin_streak.desc())
        .limit(limit)
    )
    return list(enumerate((row[0] for row in result.all()), start=1))


# ─── Purchase / renew ──────────────────────────────────────────────────


async def start_purchase(db: AsyncSession, telegram_user_id: int, plan: str) -> BotPurchaseResult:
    try:
        validate_plan_id(plan)
    except ValueError as exc:
        return BotPurchaseResult(status="invalid_plan", detail=str(exc))

    if plan == "free":
        return BotPurchaseResult(
            status="invalid_plan",
            detail="무료 체험 요금제는 USDT 결제가 필요하지 않습니다.",
        )

    plan_def = get_plan(plan)
    if plan_def is None:
        return BotPurchaseResult(status="invalid_plan", detail="유효하지 않은 요금제입니다.")

    if not check_rate_limit(f"tg:{telegram_user_id}", "bot_purchase", max_attempts=5, window_seconds=60):
        return BotPurchaseResult(
            status="rate_limited",
            detail="너무 많은 요청입니다. 잠시 후 다시 시도해주세요.",
        )

    billing = _billing_interval_for(plan)
    price = plan_def["prices_usdt"][billing]
    payment_ref = purchase_service.generate_payment_ref()
    identifier = tg_identifier(telegram_user_id)

    try:
        await purchase_service.upsert_pending_tenant(db, plan=plan, payment_ref=payment_ref, phone=identifier)
    except purchase_service.PurchaseConflict as exc:
        return BotPurchaseResult(status="already_active", detail=str(exc))

    return BotPurchaseResult(
        status="ok",
        plan=plan,
        plan_name=plan_def["name"],
        billing=billing,
        amount_usdt=price,
        wallet_address=settings.usdt_wallet_address,
        payment_ref=payment_ref,
    )


async def start_renew(db: AsyncSession, telegram_user_id: int) -> BotPurchaseResult:
    tenant = await _resolve_tenant(db, telegram_user_id)
    if tenant is None or tenant.plan not in PLAN_CATALOG or tenant.plan == "free":
        return BotPurchaseResult(
            status="no_prior_plan",
            detail="갱신할 기존 요금제가 없습니다. 먼저 요금제를 선택해 결제해주세요.",
        )
    return await start_purchase(db, telegram_user_id, tenant.plan)


# ─── Payment check / claim ─────────────────────────────────────────────


async def check_and_claim(db: AsyncSession, telegram_user_id: int) -> ClaimResult:
    tenant = await _resolve_tenant(db, telegram_user_id)
    if tenant is None:
        return ClaimResult(status="no_tenant", detail="구매 내역이 없습니다. 먼저 요금제를 결제해주세요.")

    if tenant.subscription_status != "active":
        return ClaimResult(
            status="pending",
            detail="아직 입금이 확인되지 않았습니다. 입금 후 평균 5~10분 내로 자동 처리됩니다.",
        )

    result = await db.execute(
        select(PaymentRecord)
        .where(PaymentRecord.tenant_id == tenant.id, PaymentRecord.claimed == False)  # noqa: E712
        .order_by(PaymentRecord.created_at.desc())
        .limit(1)
    )
    payment = result.scalar_one_or_none()
    if payment is None or payment.api_key_id is None:
        return ClaimResult(
            status="no_payment",
            detail="이미 발급된 API 키가 있는지 '내 API 키' 메뉴에서 확인해주세요.",
        )

    api_key = await db.get(APIKey, payment.api_key_id)
    if api_key is None:
        return ClaimResult(status="no_payment", detail="API 키를 찾을 수 없습니다. 고객센터로 문의해주세요.")

    payment.claimed = True
    await db.commit()

    logger.info("bot_payment_claimed", telegram_user_id=telegram_user_id, tenant_id=tenant.id)
    return ClaimResult(status="claimed", api_key=api_key.key)


# ─── Purchase history ──────────────────────────────────────────────────


async def list_purchase_history(db: AsyncSession, telegram_user_id: int, limit: int = 10) -> list[PaymentRecord]:
    tenant = await _resolve_tenant(db, telegram_user_id)
    if tenant is None:
        return []
    result = await db.execute(
        select(PaymentRecord)
        .where(PaymentRecord.tenant_id == tenant.id)
        .order_by(PaymentRecord.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
