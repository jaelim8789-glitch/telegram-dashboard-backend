import json
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reply_macro import ReplyMacro, ReplyMacroLog


# ─── MACRO CRUD ───────────────────────────────────────────────────────

async def create_macro(
    db: AsyncSession,
    account_id: str,
    target_chats: list[str] | object | None = None,
    message_content: str | None = None,
    *,
    name: str = "macro",
    media_path: str | None = None,
    schedule_type: str = "interval",
    interval_hours: int = 24,
    fixed_time: str | None = None,
    max_sends_per_day: int = 10,
    is_active: bool = True,
    macro_data: object = None,
) -> ReplyMacro:
    if macro_data is not None:
        data = macro_data
    elif not isinstance(target_chats, list):
        data = target_chats
        target_chats = None
        message_content = None
    else:
        data = None

    if data is not None:
        target_chats = data.target_chats
        message_content = data.message_content
        name = getattr(data, "name", name)
        media_path = getattr(data, "media_path", media_path)
        schedule_type = getattr(data, "schedule_type", schedule_type)
        interval_hours = getattr(data, "interval_hours", interval_hours)
        fixed_time = getattr(data, "fixed_time", fixed_time)
        max_sends_per_day = getattr(data, "max_sends_per_day", max_sends_per_day)
        is_active = getattr(data, "is_active", is_active)

    macro = ReplyMacro(
        account_id=account_id,
        name=name,
        target_chats=json.dumps(target_chats or []),
        message_content=message_content or "",
        media_path=media_path,
        schedule_type=schedule_type,
        interval_hours=interval_hours,
        fixed_time=fixed_time,
        max_sends_per_day=max_sends_per_day,
        is_active=is_active,
    )
    db.add(macro)
    await db.commit()
    await db.refresh(macro)
    return macro


async def list_macros(db: AsyncSession, account_id: str) -> list[ReplyMacro]:
    result = await db.execute(
        select(ReplyMacro)
        .where(ReplyMacro.account_id == account_id)
        .order_by(ReplyMacro.updated_at.desc())
    )
    return list(result.scalars().all())


async def get_macro(db: AsyncSession, macro_id: str) -> ReplyMacro | None:
    return await db.get(ReplyMacro, macro_id)


async def delete_macro(db: AsyncSession, macro: ReplyMacro) -> None:
    await db.delete(macro)
    await db.commit()


# ─── Random Reply Helpers ───────────────────────────────────────────────

async def get_used_targets(macro: ReplyMacro) -> list[dict]:
    try:
        return json.loads(macro.used_targets) if macro.used_targets else []
    except (json.JSONDecodeError, TypeError):
        return []


async def add_used_target(db: AsyncSession, macro: ReplyMacro, chat_id: str, user_id: str) -> None:
    used = await get_used_targets(macro)
    used.append({"chat_id": chat_id, "user_id": user_id})
    macro.used_targets = json.dumps(used)
    await db.commit()


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
    replied_user_id: str | None = None,
    replied_msg_id: int | None = None,
) -> ReplyMacroLog:
    log = ReplyMacroLog(
        macro_id=macro_id,
        account_id=account_id,
        target_chat_id=target_chat_id,
        replied_user_id=replied_user_id,
        replied_msg_id=replied_msg_id,
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


async def list_active_macros_due(db: AsyncSession) -> list[ReplyMacro]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result = await db.execute(
        select(ReplyMacro).where(
            ReplyMacro.is_active == True,  # noqa: E712
            ReplyMacro.last_sent_at.is_(None) | (ReplyMacro.last_sent_at <= now),
        )
    )
    return list(result.scalars().all())


async def claim_macro_dispatch(db: AsyncSession, macro_id: str, observed_last_sent_at: datetime | None) -> bool:
    result = await db.execute(
        select(ReplyMacro).where(
            ReplyMacro.id == macro_id,
            ReplyMacro.is_active == True,  # noqa: E712
        ).with_for_update()
    )
    macro = result.scalar_one_or_none()
    if macro is None:
        return False
    if macro.last_sent_at != observed_last_sent_at:
        return False
    macro.last_sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    return True


async def mark_macro_sent(db: AsyncSession, macro: ReplyMacro) -> ReplyMacro:
    macro.last_sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    await db.refresh(macro)
    return macro
