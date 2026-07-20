from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, require_admin
from app.database import get_db
from app.core.logging import get_logger
from app.models.token import TokenBalance, StreakRecord, TokenTransaction
from app.crud import user as user_crud

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["tokens"])


FREE_TIER_TOKENS = 1000


async def _get_or_create_balance(db: AsyncSession, user_id: str) -> TokenBalance:
    result = await db.execute(select(TokenBalance).where(TokenBalance.user_id == user_id))
    balance = result.scalar_one_or_none()
    if balance is None:
        balance = TokenBalance(user_id=user_id, balance=FREE_TIER_TOKENS, lifetime_earned=FREE_TIER_TOKENS)
        db.add(balance)
        await db.commit()
        await db.refresh(balance)
    return balance


async def _get_or_create_streak(db: AsyncSession, user_id: str) -> StreakRecord:
    result = await db.execute(select(StreakRecord).where(StreakRecord.user_id == user_id))
    streak = result.scalar_one_or_none()
    if streak is None:
        streak = StreakRecord(user_id=user_id)
        db.add(streak)
        await db.commit()
        await db.refresh(streak)
    return streak


async def _record_transaction(
    db: AsyncSession,
    user_id: str,
    amount: int,
    balance_after: int,
    reason: str,
    reference_id: str | None = None,
    memo: str | None = None,
):
    tx = TokenTransaction(
        user_id=user_id,
        amount=amount,
        balance_after=balance_after,
        reason=reason,
        reference_id=reference_id,
        memo=memo,
    )
    db.add(tx)
    await db.flush()


@router.get("/tokens/balance")
async def get_token_balance(
    db: AsyncSession = Depends(get_db),
    identity = Depends(get_current_identity),
):
    """현재 토큰 잔액과 출석 정보를 반환합니다."""
    user = await user_crud.get_user(db, identity.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")

    balance = await _get_or_create_balance(db, user.id)
    streak = await _get_or_create_streak(db, user.id)

    return {
        "balance": balance.balance,
        "lifetime_earned": balance.lifetime_earned,
        "streak": streak.current_streak,
        "longest_streak": streak.longest_streak,
        "last_checkin": streak.last_checkin_date,
    }


@router.post("/tokens/checkin")
async def daily_checkin(
    db: AsyncSession = Depends(get_db),
    identity = Depends(get_current_identity),
):
    """출석 체크인. 연속 출석일에 따라 보상이 차등 지급됩니다.
    3일=30, 7일=100, 14일=300, 30일=1000, 365일=10000
    """
    user = await user_crud.get_user(db, identity.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    reward_map: dict[int, int] = {3: 30, 7: 100, 14: 300, 30: 1000, 365: 10000}

    streak = await _get_or_create_streak(db, user.id)

    if streak.last_checkin_date == today:
        # 이미 오늘 출석함
        balance = await _get_or_create_balance(db, user.id)
        return {"streak": streak.current_streak, "reward": 0, "balance": balance.balance}

    # 연속일 계산
    yesterday = datetime.now(timezone.utc)
    yesterday = yesterday.replace(day=yesterday.day - 1).strftime("%Y-%m-%d")

    if streak.last_checkin_date == yesterday:
        streak.current_streak += 1
    else:
        streak.current_streak = 1

    streak.longest_streak = max(streak.longest_streak, streak.current_streak)
    streak.last_checkin_date = today

    reward = reward_map.get(streak.current_streak, 0)

    balance = await _get_or_create_balance(db, user.id)
    if reward > 0:
        balance.balance += reward
        balance.lifetime_earned += reward
        await _record_transaction(db, user.id, reward, balance.balance, "checkin", memo=f"출석 {streak.current_streak}일")

    await db.commit()

    logger.info("token_checkin", user_id=user.id, streak=streak.current_streak, reward=reward)

    return {"streak": streak.current_streak, "reward": reward, "balance": balance.balance}


@router.post("/tokens/spend")
async def spend_tokens(
    body: dict,
    db: AsyncSession = Depends(get_db),
    identity = Depends(get_current_identity),
):
    """토큰을 사용합니다 (AI 호출 등).

    Request body: {"amount": 50, "reason": "ai_chat", "reference_id": "..."}
    """
    amount = body.get("amount", 0)
    reason = body.get("reason", "unknown")
    reference_id = body.get("reference_id")

    if amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효하지 않은 토큰 수량입니다.")

    user = await user_crud.get_user(db, identity.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")

    balance = await _get_or_create_balance(db, user.id)
    if balance.balance < amount:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="토큰이 부족합니다.")

    balance.balance -= amount
    await _record_transaction(db, user.id, -amount, balance.balance, reason, reference_id)
    await db.commit()

    logger.info("token_spent", user_id=user.id, amount=amount, reason=reason)
    return {"balance": balance.balance, "spent": amount}


@router.get("/tokens/transactions")
async def get_transactions(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    identity = Depends(get_current_identity),
):
    """토큰 사용 내역을 조회합니다."""
    user = await user_crud.get_user(db, identity.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")

    result = await db.execute(
        select(TokenTransaction)
        .where(TokenTransaction.user_id == user.id)
        .order_by(TokenTransaction.created_at.desc())
        .limit(limit)
    )
    txs = result.scalars().all()
    return [
        {
            "id": tx.id,
            "amount": tx.amount,
            "balance_after": tx.balance_after,
            "reason": tx.reason,
            "memo": tx.memo,
            "created_at": tx.created_at.isoformat() if tx.created_at else None,
        }
        for tx in txs
    ]
