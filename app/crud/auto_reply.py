from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auto_reply import AutoReplyLog, AutoReplyRule
from app.schemas.auto_reply import AutoReplyRuleCreate, AutoReplyRuleUpdate


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def create_rule(db: AsyncSession, account_id: str, data: AutoReplyRuleCreate) -> AutoReplyRule:
    rule = AutoReplyRule(account_id=account_id, **data.model_dump())
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


async def list_rules(db: AsyncSession, account_id: str) -> list[AutoReplyRule]:
    result = await db.execute(
        select(AutoReplyRule).where(AutoReplyRule.account_id == account_id).order_by(AutoReplyRule.created_at.desc())
    )
    return list(result.scalars().all())


async def list_active_rules(db: AsyncSession, account_id: str) -> list[AutoReplyRule]:
    result = await db.execute(
        select(AutoReplyRule).where(AutoReplyRule.account_id == account_id, AutoReplyRule.is_active.is_(True))
    )
    return list(result.scalars().all())


async def get_rule(db: AsyncSession, rule_id: str) -> AutoReplyRule | None:
    return await db.get(AutoReplyRule, rule_id)


async def update_rule(db: AsyncSession, rule: AutoReplyRule, data: AutoReplyRuleUpdate) -> AutoReplyRule:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)
    await db.commit()
    await db.refresh(rule)
    return rule


async def delete_rule(db: AsyncSession, rule: AutoReplyRule) -> None:
    await db.delete(rule)
    await db.commit()


async def create_log(
    db: AsyncSession,
    *,
    rule_id: str,
    account_id: str,
    chat_id: str,
    user_id: str,
    user_name: str | None,
    trigger_message: str,
    reply_sent: str,
    status: str,
) -> AutoReplyLog:
    log = AutoReplyLog(
        rule_id=rule_id,
        account_id=account_id,
        chat_id=chat_id,
        user_id=user_id,
        user_name=user_name,
        trigger_message=trigger_message,
        reply_sent=reply_sent,
        status=status,
    )
    db.add(log)
    await db.commit()
    await db.refresh(log)
    return log


async def list_logs(
    db: AsyncSession, account_id: str, *, rule_id: str | None = None, status: str | None = None
) -> list[AutoReplyLog]:
    query = select(AutoReplyLog).where(AutoReplyLog.account_id == account_id).order_by(AutoReplyLog.created_at.desc())
    if rule_id:
        query = query.where(AutoReplyLog.rule_id == rule_id)
    if status:
        query = query.where(AutoReplyLog.status == status)
    result = await db.execute(query)
    return list(result.scalars().all())


async def get_last_successful_reply_time(db: AsyncSession, rule_id: str, user_id: str) -> datetime | None:
    result = await db.execute(
        select(AutoReplyLog.created_at)
        .where(AutoReplyLog.rule_id == rule_id, AutoReplyLog.user_id == user_id, AutoReplyLog.status == "success")
        .order_by(AutoReplyLog.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def count_successful_replies_today(db: AsyncSession, rule_id: str) -> int:
    day_start = utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(func.count())
        .select_from(AutoReplyLog)
        .where(
            AutoReplyLog.rule_id == rule_id,
            AutoReplyLog.status == "success",
            AutoReplyLog.created_at >= day_start,
            AutoReplyLog.created_at < day_start + timedelta(days=1),
        )
    )
    return result.scalar_one()
