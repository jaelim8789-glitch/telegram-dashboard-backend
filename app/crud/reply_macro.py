import json
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reply_macro import ReplyMacro, ReplyMacroLog


# ─── MACRO CRUD ───────────────────────────────────────────────────────

async def create_macro(
    db: AsyncSession, account_id: str, name: str, target_chats: list[str], message_content: str, media_path: str | None = None
) -> ReplyMacro:
    macro = ReplyMacro(
        account_id=account_id,
        name=name,
        target_chats=json.dumps(target_chats),
        message_content=message_content,
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