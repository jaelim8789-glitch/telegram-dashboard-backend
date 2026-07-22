import csv
import io
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, and_, cast, Date
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.models.referral import (
    DistributorLevel, ReferralAuditLog, ReferralBadge, ReferralCode,
    ReferralCommission, ReferralConfig, ReferralPayout,
)
from app.models.tenant import Tenant

logger = get_logger(__name__)

# ── 50% 기본 커미션율 + 등급별 상향 ──
DEFAULT_TIERS: list[tuple[int, float, str, int]] = [
    (0, 0.50, "기본", 1),
    (3, 0.55, "Pro", 2),
    (7, 0.60, "VIP", 3),
    (15, 0.65, "파트너", 4),
    (30, 0.70, "마스터", 5),
]
# 2-tier: 직계 추천인의 추천인에게 지급되는 2차 커미션율
TIER2_RATE = 0.15
DEFAULT_MIN_PAYOUT = "100"
INSTANT_PAYOUT_FEE_RATE = 0.05
INSTANT_PAYOUT_MIN = 1000

# ── 업적 배지 정의 ──
BADGE_DEFINITIONS: list[dict] = [
    {"key": "first_referral", "label": "첫 추천", "desc": "첫 번째 추천인을 유치하세요", "icon": "🎯", "min_refs": 1},
    {"key": "bronze", "label": "브론즈 총판", "desc": "5명 추천 달성", "icon": "🥉", "min_refs": 5},
    {"key": "silver", "label": "실버 총판", "desc": "10명 추천 달성", "icon": "🥈", "min_refs": 10},
    {"key": "gold", "label": "골드 총판", "desc": "25명 추천 달성", "icon": "🥇", "min_refs": 25},
    {"key": "platinum", "label": "플래티넘 총판", "desc": "50명 추천 달성", "icon": "💎", "min_refs": 50},
    {"key": "diamond", "label": "다이아몬드 총판", "desc": "100명 추천 달성", "icon": "💠", "min_refs": 100},
    {"key": "first_commission", "label": "첫 커미션", "desc": "첫 커미션을 획득하세요", "icon": "💰", "min_earnings": 1000},
    {"key": "big_earner", "label": "빅 어너", "desc": "누적 커미션 100만원 달성", "icon": "🏆", "min_earnings": 1_000_000},
    {"key": "millionaire", "label": "밀리어네어", "desc": "누적 커미션 1000만원 달성", "icon": "👑", "min_earnings": 10_000_000},
]

# ── 주간 미션 정의 ──
WEEKLY_MISSIONS: list[dict] = [
    {"key": "week_ref_3", "label": "이번 주 3명 추천", "reward": "보너스 5%", "target": 3},
    {"key": "week_ref_5", "label": "이번 주 5명 추천", "reward": "보너스 10%", "target": 5},
    {"key": "week_earn_50k", "label": "이번 주 5만원 커미션", "reward": "2,000원 추가", "target_earnings": 50_000},
    {"key": "week_earn_200k", "label": "이번 주 20만원 커미션", "reward": "10,000원 추가", "target_earnings": 200_000},
]


async def get_config(db: AsyncSession, key: str, default: str = "") -> str:
    result = await db.execute(select(ReferralConfig).where(ReferralConfig.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else default


async def set_config(db: AsyncSession, key: str, value: str) -> None:
    result = await db.execute(select(ReferralConfig).where(ReferralConfig.key == key))
    row = result.scalar_one_or_none()
    if row:
        row.value = value
    else:
        row = ReferralConfig(key=key, value=value)
        db.add(row)
    await db.commit()


async def _load_tiers(db: AsyncSession) -> list[tuple[int, float, str, int]]:
    raw = await get_config(db, "tiers")
    if not raw:
        return default_tiers()
    try:
        import json
        parsed = json.loads(raw)
        return [(int(t["min_refs"]), float(t["rate"]), t["label"], t.get("level", 1)) for t in parsed]
    except Exception:
        return default_tiers()


def default_tiers() -> list[tuple[int, float, str, int]]:
    return [(t[0], t[1], t[2], t[3]) for t in DEFAULT_TIERS]


async def get_min_payout(db: AsyncSession) -> int:
    raw = await get_config(db, "min_payout", DEFAULT_MIN_PAYOUT)
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 100


def _get_tier(referral_count: int, tiers: list[tuple[int, float, str, int]]) -> tuple[float, str, int]:
    rate = DEFAULT_TIERS[0][1]
    label = DEFAULT_TIERS[0][2]
    level = DEFAULT_TIERS[0][3]
    for min_refs, tier_rate, tier_label, tier_level in tiers:
        if referral_count >= min_refs and tier_rate > rate:
            rate = tier_rate
            label = tier_label
            level = tier_level
    return rate, label, level


async def get_referrer_tier(db: AsyncSession, referrer_id: str) -> tuple[float, str, int]:
    result = await db.execute(
        select(func.count(ReferralCommission.id))
        .where(
            ReferralCommission.referrer_id == referrer_id,
            ReferralCommission.status.in_(["pending", "paid"]),
        )
    )
    count = result.scalar_one_or_none() or 0
    tiers = await _load_tiers(db)
    return _get_tier(count, tiers)


async def get_referral_count(db: AsyncSession, referrer_id: str) -> int:
    result = await db.execute(
        select(func.count(func.distinct(ReferralCommission.referred_user_id)))
        .where(
            ReferralCommission.referrer_id == referrer_id,
            ReferralCommission.status.in_(["pending", "paid"]),
        )
    )
    return result.scalar_one_or_none() or 0


async def _get_or_create_distributor_level(db: AsyncSession, referrer_id: str) -> DistributorLevel:
    result = await db.execute(select(DistributorLevel).where(DistributorLevel.tenant_id == referrer_id))
    dl = result.scalar_one_or_none()
    if dl:
        dl.total_referrals = await get_referral_count(db, referrer_id)
        earnings = await db.execute(
            select(func.coalesce(func.sum(ReferralCommission.commission_amount), 0))
            .where(
                ReferralCommission.referrer_id == referrer_id,
                ReferralCommission.status.in_(["pending", "paid"]),
            )
        )
        dl.total_earnings = earnings.scalar_one_or_none() or 0

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        week_ago = now - timedelta(days=7)
        weekly = await db.execute(
            select(func.count(ReferralCommission.id))
            .where(
                ReferralCommission.referrer_id == referrer_id,
                ReferralCommission.created_at >= week_ago,
            )
        )
        dl.weekly_referrals = weekly.scalar_one_or_none() or 0
        dl.weekly_reset_at = now
    else:
        dl = DistributorLevel(tenant_id=referrer_id, level=1)
        db.add(dl)

    _, _, level = await get_referrer_tier(db, referrer_id)
    dl.level = level
    await db.commit()
    await db.refresh(dl) if dl.id else None
    return dl


async def _check_badges(db: AsyncSession, referrer_id: str) -> list[str]:
    ref_count = await get_referral_count(db, referrer_id)
    earnings = await db.execute(
        select(func.coalesce(func.sum(ReferralCommission.commission_amount), 0))
        .where(
            ReferralCommission.referrer_id == referrer_id,
            ReferralCommission.status.in_(["pending", "paid"]),
        )
    )
    total_earnings = earnings.scalar_one_or_none() or 0

    earned = await db.execute(
        select(ReferralBadge.badge_key).where(ReferralBadge.tenant_id == referrer_id)
    )
    existing = {row[0] for row in earned.all()}

    new_badges: list[str] = []
    for bd in BADGE_DEFINITIONS:
        if bd["key"] in existing:
            continue
        earned_flag = False
        if "min_refs" in bd and ref_count >= bd["min_refs"]:
            earned_flag = True
        if "min_earnings" in bd and total_earnings >= bd["min_earnings"]:
            earned_flag = True
        if earned_flag:
            db.add(ReferralBadge(tenant_id=referrer_id, badge_key=bd["key"]))
            new_badges.append(bd["key"])

    if new_badges:
        await db.commit()

    return list(existing) + new_badges


async def create_commission(
    db: AsyncSession,
    referred_tenant_id: str,
    source_payment_id: str,
    source_type: str,
    amount: int,
    webhook_urls: list[str] | None = None,
) -> ReferralCommission | None:
    tenant = await db.get(Tenant, referred_tenant_id)
    if not tenant or not tenant.referred_by:
        return None

    referrer_id = tenant.referred_by
    result = await db.execute(select(ReferralCode).where(ReferralCode.owner_id == referrer_id))
    ref_code = result.scalar_one_or_none()
    if not ref_code:
        return None
    if referrer_id == referred_tenant_id:
        return None

    existing = await db.execute(
        select(ReferralCommission).where(
            ReferralCommission.referrer_id == referrer_id,
            ReferralCommission.referred_user_id == referred_tenant_id,
            ReferralCommission.source_payment_id == source_payment_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        logger.info("commission_duplicate_skipped", referrer_id=referrer_id, source_payment_id=source_payment_id)
        return None

    # Level 1 commission (50% base, up to 70%)
    rate, tier_label, dist_level = await get_referrer_tier(db, referrer_id)
    commission_amount = max(1, int(amount * rate))

    commission = ReferralCommission(
        referrer_id=referrer_id,
        referred_user_id=referred_tenant_id,
        source_payment_id=source_payment_id,
        source_type=source_type,
        level=1,
        amount=amount,
        commission_rate=rate,
        commission_amount=commission_amount,
        status="pending",
    )
    db.add(commission)
    await db.commit()
    await db.refresh(commission)
    logger.info("referral_commission_created", referrer_id=referrer_id, referred_user_id=referred_tenant_id, amount=commission_amount, rate=rate)

    # 2-tier commission: give TIER2_RATE to the referrer's referrer
    referrer_tenant = await db.get(Tenant, referrer_id)
    if referrer_tenant and referrer_tenant.referred_by:
        tier2_referrer_id = referrer_tenant.referred_by
        if tier2_referrer_id != referred_tenant_id and tier2_referrer_id != referrer_id:
            tier2_amount = max(1, int(amount * TIER2_RATE))
            tier2_existing = await db.execute(
                select(ReferralCommission).where(
                    ReferralCommission.referrer_id == tier2_referrer_id,
                    ReferralCommission.referred_user_id == referred_tenant_id,
                    ReferralCommission.source_payment_id == source_payment_id,
                )
            )
            if tier2_existing.scalar_one_or_none() is None:
                tier2_commission = ReferralCommission(
                    referrer_id=tier2_referrer_id,
                    referred_user_id=referred_tenant_id,
                    source_payment_id=source_payment_id,
                    source_type=source_type,
                    level=2,
                    amount=amount,
                    commission_rate=TIER2_RATE,
                    commission_amount=tier2_amount,
                    status="pending",
                )
                db.add(tier2_commission)
                await db.commit()
                logger.info("tier2_commission_created", referrer_id=tier2_referrer_id, amount=tier2_amount)

                tier2_referrer = await db.get(Tenant, tier2_referrer_id)
                if tier2_referrer and tier2_referrer.telegram_chat_id:
                    await _send_telegram_notification(
                        tier2_referrer.telegram_chat_id,
                        f"2차 커미션이 발생했습니다! (추천인의 추천인)\n금액: {tier2_amount}원\n수수료율: {int(TIER2_RATE * 100)}%",
                    )

    # Notifications
    if webhook_urls:
        await _send_webhook(webhook_urls, "commission.created", {
            "commission_id": commission.id,
            "referrer_id": referrer_id,
            "amount": commission_amount,
            "rate": rate,
        })

    referrer = await db.get(Tenant, referrer_id)
    if referrer and referrer.telegram_chat_id:
        await _send_telegram_notification(
            referrer.telegram_chat_id,
            f"새로운 추천인 커미션이 발생했습니다!\n\n"
            f"금액: {commission_amount}원\n"
            f"수수료율: {int(rate * 100)}%\n"
            f"등급: {tier_label}\n"
            f"상태: 지급 대기 중",
        )

    # Check badges
    await _check_badges(db, referrer_id)
    await _get_or_create_distributor_level(db, referrer_id)

    return commission


async def create_recurring_commission(
    db: AsyncSession,
    referred_tenant_id: str,
    source_payment_id: str,
    amount: int,
) -> ReferralCommission | None:
    return await create_commission(
        db, referred_tenant_id, source_payment_id, "recurring", amount,
    )


async def process_payouts(
    db: AsyncSession,
    min_amount: int | None = None,
    tenant_id: str | None = None,
) -> tuple[int, int]:
    if min_amount is None:
        min_amount = await get_min_payout(db)

    query = select(
        ReferralCommission.referrer_id,
        func.sum(ReferralCommission.commission_amount).label("total"),
    ).where(ReferralCommission.status == "pending")
    if tenant_id:
        query = query.where(ReferralCommission.referrer_id == tenant_id)
    result = await db.execute(query.group_by(ReferralCommission.referrer_id).having(func.sum(ReferralCommission.commission_amount) >= min_amount))
    rows = result.all()

    payouts_created = 0
    total_amount = 0

    for row in rows:
        referrer_id = row.referrer_id
        amount = row.total
        payout = ReferralPayout(
            referrer_id=referrer_id,
            amount=amount,
            fee=0,
            payout_type="standard",
            status="pending",
        )
        db.add(payout)
        payouts_created += 1
        total_amount += amount

    if payouts_created > 0:
        await db.commit()

    return payouts_created, total_amount


async def create_instant_payout(
    db: AsyncSession,
    referrer_id: str,
    amount: int | None = None,
) -> dict:
    tenant = await db.get(Tenant, referrer_id)
    if not tenant or not tenant.is_distributor:
        return {"success": False, "message": "총판만 즉시 지급을 사용할 수 있습니다.", "payout_id": None, "amount": 0, "fee": 0, "net_amount": 0}

    result = await db.execute(
        select(func.coalesce(func.sum(ReferralCommission.commission_amount), 0))
        .where(
            ReferralCommission.referrer_id == referrer_id,
            ReferralCommission.status == "pending",
        )
    )
    pending_total = result.scalar_one_or_none() or 0

    if pending_total < INSTANT_PAYOUT_MIN:
        return {"success": False, "message": f"즉시 지급은 최소 {INSTANT_PAYOUT_MIN}원부터 가능합니다.", "payout_id": None, "amount": 0, "fee": 0, "net_amount": 0}

    payout_amount = min(amount, pending_total) if amount else pending_total
    fee = max(1, int(payout_amount * INSTANT_PAYOUT_FEE_RATE))
    net_amount = payout_amount - fee

    payout = ReferralPayout(
        referrer_id=referrer_id,
        amount=payout_amount,
        fee=fee,
        payout_type="instant",
        status="pending",
    )
    db.add(payout)
    await db.commit()
    await db.refresh(payout)

    await log_audit(db, "payout.instant", actor_id=referrer_id, target_id=payout.id, details=f"Instant payout {payout_amount}, fee={fee}, net={net_amount}")

    return {
        "success": True,
        "payout_id": payout.id,
        "amount": payout_amount,
        "fee": fee,
        "net_amount": net_amount,
        "message": f"즉시 지급 요청 완료! {payout_amount}원 중 수수료 {fee}원 차감 후 {net_amount}원이 지급됩니다.",
    }


async def log_audit(
    db: AsyncSession,
    action: str,
    actor_id: str | None = None,
    target_id: str | None = None,
    details: str = "",
) -> None:
    db.add(ReferralAuditLog(
        action=action,
        actor_id=actor_id,
        target_id=target_id,
        details=details,
    ))
    await db.commit()


async def approve_payout(db: AsyncSession, payout_id: str, actor_id: str | None = None) -> bool:
    payout = await db.get(ReferralPayout, payout_id)
    if not payout or payout.status != "pending":
        return False

    payout.status = "completed"
    payout.paid_at = datetime.now(timezone.utc).replace(tzinfo=None)

    await db.execute(
        ReferralCommission.__table__.update()
        .where(
            ReferralCommission.referrer_id == payout.referrer_id,
            ReferralCommission.status == "pending",
        )
        .values(status="paid")
    )
    await db.commit()

    await log_audit(
        db, "payout.approve",
        actor_id=actor_id,
        target_id=payout_id,
        details=f"Payout {payout_id} approved, amount={payout.amount}, fee={payout.fee}",
    )

    referrer = await db.get(Tenant, payout.referrer_id)
    if referrer and referrer.telegram_chat_id:
        await _send_telegram_notification(
            referrer.telegram_chat_id,
            f"{payout.amount}원이 정산 완료되었습니다!\n\n감사합니다.",
        )

    return True


async def cancel_commission(db: AsyncSession, commission_id: str, actor_id: str | None = None) -> bool:
    commission = await db.get(ReferralCommission, commission_id)
    if not commission or commission.status == "cancelled":
        return False

    commission.status = "cancelled"
    await db.commit()

    await log_audit(
        db, "commission.cancel",
        actor_id=actor_id,
        target_id=commission_id,
        details=f"Commission {commission_id} cancelled, amount={commission.commission_amount}",
    )

    referrer = await db.get(Tenant, commission.referrer_id)
    if referrer and referrer.telegram_chat_id:
        await _send_telegram_notification(
            referrer.telegram_chat_id,
            f"커미션이 취소되었습니다.\n금액: {commission.commission_amount}원\n사유: 결제 취소/환불",
        )

    return True


async def cancel_commissions_by_payment(db: AsyncSession, source_payment_id: str, actor_id: str | None = None) -> int:
    result = await db.execute(
        select(ReferralCommission).where(
            ReferralCommission.source_payment_id == source_payment_id,
            ReferralCommission.status == "pending",
        )
    )
    commissions = list(result.scalars().all())

    cancelled = 0
    for c in commissions:
        c.status = "cancelled"
        cancelled += 1
        await log_audit(
            db, "commission.cancel",
            actor_id=actor_id,
            target_id=c.id,
            details=f"Auto-cancel by payment {source_payment_id}, amount={c.commission_amount}",
        )

    if cancelled > 0:
        await db.commit()

    return cancelled


async def get_pending_payouts(db: AsyncSession) -> list[ReferralPayout]:
    result = await db.execute(
        select(ReferralPayout)
        .where(ReferralPayout.status == "pending")
        .order_by(ReferralPayout.created_at.desc())
    )
    return list(result.scalars().all())


async def get_my_payouts(db: AsyncSession, referrer_id: str) -> list[ReferralPayout]:
    result = await db.execute(
        select(ReferralPayout)
        .where(ReferralPayout.referrer_id == referrer_id)
        .order_by(ReferralPayout.created_at.desc())
    )
    return list(result.scalars().all())


async def get_min_payout_threshold(db: AsyncSession) -> int:
    return await get_min_payout(db)


async def get_leaderboard(db: AsyncSession, limit: int = 20) -> list[dict]:
    tiers = await _load_tiers(db)

    result = await db.execute(
        select(
            ReferralCommission.referrer_id,
            func.count(ReferralCommission.id).label("ref_count"),
            func.coalesce(func.sum(ReferralCommission.commission_amount), 0).label("total_earned"),
        )
        .where(ReferralCommission.status.in_(["pending", "paid"]))
        .group_by(ReferralCommission.referrer_id)
        .order_by(func.sum(ReferralCommission.commission_amount).desc())
        .limit(limit)
    )
    rows = result.all()

    entries = []
    for rank, row in enumerate(rows, 1):
        tenant = await db.get(Tenant, row.referrer_id)
        count = row.ref_count
        rate, tier_label, dist_level = _get_tier(count, tiers)
        entries.append({
            "rank": rank,
            "referrer_id": row.referrer_id,
            "phone": tenant.phone if tenant else "unknown",
            "referral_count": count,
            "total_commission_earned": row.total_earned,
            "tier": tier_label,
            "level": dist_level,
        })
    return entries


async def get_stats(db: AsyncSession, days: int = 30) -> dict:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    result = await db.execute(
        select(
            cast(Tenant.created_at, Date).label("date"),
            func.count(Tenant.id).label("signups"),
        )
        .where(Tenant.created_at >= cutoff)
        .group_by(cast(Tenant.created_at, Date))
        .order_by(cast(Tenant.created_at, Date))
    )
    signup_rows = result.all()
    signup_map = {str(r.date): r.signups for r in signup_rows}

    result = await db.execute(
        select(
            cast(ReferralCommission.created_at, Date).label("date"),
            func.coalesce(func.sum(ReferralCommission.commission_amount), 0).label("total"),
        )
        .where(ReferralCommission.created_at >= cutoff)
        .group_by(cast(ReferralCommission.created_at, Date))
        .order_by(cast(ReferralCommission.created_at, Date))
    )
    commission_rows = result.all()
    commission_map = {str(r.date): r.total for r in commission_rows}

    daily = []
    for i in range(days - 1, -1, -1):
        d = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=i)).strftime("%Y-%m-%d")
        daily.append({
            "date": d,
            "signups": signup_map.get(d, 0),
            "commissions": commission_map.get(d, 0),
        })

    total_referrers = await db.execute(
        select(func.count(func.distinct(ReferralCommission.referrer_id)))
    )

    total_referred = await db.execute(
        select(func.count(ReferralCommission.id))
    )

    pending_count = await db.execute(
        select(func.count(ReferralCommission.id))
        .where(ReferralCommission.status == "pending")
    )

    paid_count = await db.execute(
        select(func.count(ReferralCommission.id))
        .where(ReferralCommission.status == "paid")
    )

    pending_amount = await db.execute(
        select(func.coalesce(func.sum(ReferralCommission.commission_amount), 0))
        .where(ReferralCommission.status == "pending")
    )

    paid_amount = await db.execute(
        select(func.coalesce(func.sum(ReferralCommission.commission_amount), 0))
        .where(ReferralCommission.status == "paid")
    )

    return {
        "total_referrers": total_referrers.scalar_one_or_none() or 0,
        "total_referred": total_referred.scalar_one_or_none() or 0,
        "total_commissions_pending": pending_count.scalar_one_or_none() or 0,
        "total_commissions_paid": paid_count.scalar_one_or_none() or 0,
        "total_commission_amount_pending": pending_amount.scalar_one_or_none() or 0,
        "total_commission_amount_paid": paid_amount.scalar_one_or_none() or 0,
        "daily": daily,
    }


async def get_my_commissions(
    db: AsyncSession,
    referrer_id: str,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    total = await db.execute(
        select(func.count(ReferralCommission.id))
        .where(ReferralCommission.referrer_id == referrer_id)
    )
    total_count = total.scalar_one_or_none() or 0

    result = await db.execute(
        select(ReferralCommission)
        .where(ReferralCommission.referrer_id == referrer_id)
        .order_by(ReferralCommission.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    commissions = list(result.scalars().all())

    items = []
    for c in commissions:
        referred = await db.get(Tenant, c.referred_user_id)
        items.append({
            "id": c.id,
            "referred_user_phone": referred.phone if referred else "unknown",
            "source_type": c.source_type,
            "amount": c.amount,
            "commission_rate": c.commission_rate,
            "commission_amount": c.commission_amount,
            "level": c.level,
            "status": c.status,
            "created_at": c.created_at,
        })
    return items, total_count


async def set_wallet_address(db: AsyncSession, tenant_id: str, wallet_address: str) -> Tenant | None:
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        return None
    tenant.wallet_address = wallet_address
    await db.commit()
    await db.refresh(tenant)
    return tenant


async def set_payout_method(db: AsyncSession, tenant_id: str, method: str, wallet_address: str | None = None) -> Tenant | None:
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        return None
    tenant.wallet_address = wallet_address if method == "wallet" else None
    await db.commit()
    await db.refresh(tenant)
    return tenant


async def get_admin_code_stats(db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(
            ReferralCode.code,
            ReferralCode.owner_id,
            ReferralCode.expires_at,
            ReferralCode.is_active,
            ReferralCode.created_at,
        )
        .order_by(ReferralCode.created_at.desc())
    )
    codes = result.all()

    items = []
    for code_row in codes:
        owner = await db.get(Tenant, code_row.owner_id)
        ref_count = await db.execute(
            select(func.count(ReferralCommission.id))
            .where(
                ReferralCommission.referrer_id == code_row.owner_id,
                ReferralCommission.status.in_(["pending", "paid"]),
            )
        )
        items.append({
            "code": code_row.code,
            "owner_phone": owner.phone if owner else "unknown",
            "used_count": ref_count.scalar_one_or_none() or 0,
            "expires_at": code_row.expires_at,
            "created_at": code_row.created_at,
        })
    return items


def generate_commissions_csv(commissions: list[dict]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "추천인ID", "추천인전화번호", "추천인전화번호", "레벨", "결제유형", "결제금액", "수수료율", "수수료금액", "상태", "생성일"])
    for c in commissions:
        writer.writerow([c["id"], c["referrer_id"], c["referrer_phone"], c["referred_user_phone"], c.get("level", 1), c["source_type"], c["amount"], c["commission_rate"], c["commission_amount"], c["status"], str(c["created_at"])])
    return output.getvalue()


def generate_stats_csv(stats: dict) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["지표", "값"])
    writer.writerow(["전체 추천인 수", stats["total_referrers"]])
    writer.writerow(["전체 추천 수", stats["total_referred"]])
    writer.writerow(["대기중 커미션 건수", stats["total_commissions_pending"]])
    writer.writerow(["지급완료 커미션 건수", stats["total_commissions_paid"]])
    writer.writerow(["대기중 커미션 금액", stats["total_commission_amount_pending"]])
    writer.writerow(["지급완료 커미션 금액", stats["total_commission_amount_paid"]])
    writer.writerow([])
    writer.writerow(["일자", "가입자수", "커미션금액"])
    for d in stats["daily"]:
        writer.writerow([d["date"], d["signups"], d["commissions"]])
    return output.getvalue()


async def run_auto_payouts() -> tuple[int, int]:
    from app.database import async_session_maker as _session_maker
    async with _session_maker() as db:
        min_amount = await get_min_payout(db)
        return await process_payouts(db, min_amount)


async def get_badges(db: AsyncSession, referrer_id: str) -> list[dict]:
    result = await db.execute(select(ReferralBadge).where(ReferralBadge.tenant_id == referrer_id))
    earned = result.scalars().all()
    earned_keys = {b.badge_key for b in earned}
    earned_map = {b.badge_key: b.earned_at for b in earned}

    return [
        {**bd, "earned_at": earned_map.get(bd["key"])}
        for bd in BADGE_DEFINITIONS
    ]


async def get_weekly_missions(db: AsyncSession, referrer_id: str) -> list[dict]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    week_ago = now - timedelta(days=7)

    weekly_refs = await db.execute(
        select(func.count(ReferralCommission.id))
        .where(
            ReferralCommission.referrer_id == referrer_id,
            ReferralCommission.level == 1,
            ReferralCommission.created_at >= week_ago,
        )
    )
    weekly_count = weekly_refs.scalar_one_or_none() or 0

    weekly_earnings = await db.execute(
        select(func.coalesce(func.sum(ReferralCommission.commission_amount), 0))
        .where(
            ReferralCommission.referrer_id == referrer_id,
            ReferralCommission.created_at >= week_ago,
        )
    )
    weekly_earned = weekly_earnings.scalar_one_or_none() or 0

    missions = []
    for m in WEEKLY_MISSIONS:
        current = 0
        target = m.get("target", 0)
        target_earnings = m.get("target_earnings", 0)

        if "target" in m:
            current = weekly_count
        elif "target_earnings" in m:
            current = weekly_earned
            target = target_earnings

        missions.append({
            "key": m["key"],
            "label": m["label"],
            "current": current,
            "target": target,
            "reward": m["reward"],
            "completed": current >= target,
        })

    return missions


async def _send_telegram_notification(chat_id: str, text: str) -> None:
    if not settings.telegram_bot_token:
        return

    try:
        from telegram import Bot
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as exc:
        logger.warning("telegram_notification_failed", chat_id=chat_id, error=str(exc))


async def _send_webhook(urls: list[str], event: str, payload: dict) -> None:
    import json
    import httpx

    body = json.dumps({"event": event, "data": payload}, ensure_ascii=False, default=str)
    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, content=body, headers={"Content-Type": "application/json"})
        except Exception as exc:
            logger.warning("webhook_failed", url=url, event=event, error=str(exc))
