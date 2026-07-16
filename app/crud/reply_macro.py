import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reply_macro import ReplyMacro, ReplyMacroLog
from app.schemas.reply_macro import ReplyMacroCreate, ReplyMacroUpdate


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── MACRO CRUD ───────────────────────────────────────────────────────


async def create_macro(
    db: AsyncSession, account_id: str, data: ReplyMacroCreate, *, media_path: str | None = None
) -> ReplyMacro:
    macro = ReplyMacro(
        account_id=account_id,
        name=data.name,
        target_chats=json.dumps(data.target_chats),
        message_content=data.message_content,
        schedule_type=data.schedule_type,
        interval_hours=data.interval_hours,
        fixed_time=data.fixed_time,
        max_sends_per_day=data.max_sends_per_day,
        is_active=data.is_active,
        reply_to_message_id=getattr(data, 'reply_to_message_id', None),
        media_path=media_path,
    )
    db.add(macro)
    await db.commit()
    await db.refresh(macro)
    return macro


async def list_macros(db: AsyncSession, account_id: str) -> list[ReplyMacro]:
    result = await db.execute(
        select(ReplyMacro)
        .where(ReplyMacro.account_id == account_id)
        .order_by(ReplyMacro.created_at.desc())
    )
    return list(result.scalars().all())


async def list_active_macros_due(db: AsyncSession) -> list[ReplyMacro]:
    """Find all active macros that are due to be sent now."""
    now = utcnow_naive()
    macros: list[ReplyMacro] = []

    result = await db.execute(
        select(ReplyMacro).where(ReplyMacro.is_active.is_(True))
    )
    all_active = list(result.scalars().all())

    for macro in all_active:
        if macro.schedule_type == "interval":
            # Interval mode: send if enough time has passed since last send
            if macro.last_sent_at is None:
                macros.append(macro)
            else:
                elapsed = now - macro.last_sent_at
                if elapsed >= timedelta(hours=macro.interval_hours):
                    macros.append(macro)
        elif macro.schedule_type == "fixed":
            # Fixed mode: send if current time matches fixed_time
            if macro.fixed_time:
                current_time = now.strftime("%H:%M")
                if current_time == macro.fixed_time:
                    # Only send once per day at that time
                    if macro.last_sent_at is None or macro.last_sent_at.date() < now.date():
                        macros.append(macro)

    return macros


async def get_macro(db: AsyncSession, macro_id: str) -> ReplyMacro | None:
    return await db.get(ReplyMacro, macro_id)


async def update_macro(db: AsyncSession, macro: ReplyMacro, data: ReplyMacroUpdate) -> ReplyMacro:
    update_data = data.model_dump(exclude_unset=True)
    if "reply_to_message_id" in update_data and update_data["reply_to_message_id"] is None:
        update_data["reply_to_message_id"] = None
    if "target_chats" in update_data and isinstance(update_data["target_chats"], list):
        update_data["target_chats"] = json.dumps(update_data["target_chats"])
    for field, value in update_data.items():
        setattr(macro, field, value)
    await db.commit()
    await db.refresh(macro)
    return macro


async def delete_macro(db: AsyncSession, macro: ReplyMacro) -> None:
    await db.delete(macro)
    await db.commit()


async def mark_macro_sent(db: AsyncSession, macro: ReplyMacro) -> None:
    """Mark macro as sent using atomic last_sent_at claim.
    
    This serves as the idempotency mechanism:
    - scheduler calls claim_macro_dispatch() before executing
    - if another tick or worker already claimed this macro, it skips
    - on restart, already-claimed macros won't re-execute
    """
    macro.last_sent_at = utcnow_naive()
    await db.commit()


async def claim_macro_dispatch(db: AsyncSession, macro_id: str, expected_last_sent_at: datetime | None) -> bool:
    """
    Atomically claim a macro for this dispatch tick using a WHERE-conditioned UPDATE.
    Only succeeds if the macro is active AND its last_sent_at still matches
    ``expected_last_sent_at`` (the value observed when list_active_macros_due
    decided this macro was due). This is truly atomic at the database level and
    works correctly in both SQLite and PostgreSQL.

    The WHERE clause must pin the previous last_sent_at value, not just
    is_active: a bare `is_active == True` condition matches on every call
    regardless of prior claims, so two concurrent ticks/workers (or the same
    macro appearing twice within one tick) would both "win" and dispatch the
    macro twice.
    """
    now = utcnow_naive()

    query = update(ReplyMacro).where(
        ReplyMacro.id == macro_id,
        ReplyMacro.is_active.is_(True),
    )
    if expected_last_sent_at is None:
        query = query.where(ReplyMacro.last_sent_at.is_(None))
    else:
        query = query.where(ReplyMacro.last_sent_at == expected_last_sent_at)

    result = await db.execute(query.values(last_sent_at=now))
    await db.commit()

    # Check if any row was actually updated
    return result.rowcount > 0


# ─── LOG CRUD ─────────────────────────────────────────────────────────


async def create_log(
    db: AsyncSession,
    *,
    macro_id: str,
    account_id: str,
    target_chat_id: str,
    message_sent: str,
    status: str,
    error_message: str | None = None,
) -> ReplyMacroLog:
    log = ReplyMacroLog(
        macro_id=macro_id,
        account_id=account_id,
        target_chat_id=target_chat_id,
        message_sent=message_sent,
        status=status,
        error_message=error_message,
    )
    db.add(log)
    await db.commit()
    await db.refresh(log)
    return log


async def list_logs(
    db: AsyncSession,
    account_id: str,
    *,
    macro_id: str | None = None,
    status: str | None = None,
) -> list[ReplyMacroLog]:
    query = (
        select(ReplyMacroLog)
        .where(ReplyMacroLog.account_id == account_id)
        .order_by(ReplyMacroLog.created_at.desc())
    )
    if macro_id:
        query = query.where(ReplyMacroLog.macro_id == macro_id)
    if status:
        query = query.where(ReplyMacroLog.status == status)
    result = await db.execute(query)
    return list(result.scalars().all())