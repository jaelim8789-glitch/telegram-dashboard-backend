from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group_search import GroupJoinLog, GroupSearchResult


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def save_search_results(
    db: AsyncSession, account_id: str, keyword: str, results: list[dict]
) -> list[GroupSearchResult]:
    """Bulk-insert search results and return them."""
    rows = [
        GroupSearchResult(
            account_id=account_id,
            keyword=keyword,
            chat_id=r["chat_id"],
            title=r["title"],
            chat_type=r.get("chat_type"),
            username=r.get("username"),
            participants_count=r.get("participants_count"),
            about=r.get("about"),
        )
        for r in results
    ]
    db.add_all(rows)
    await db.commit()
    for row in rows:
        await db.refresh(row)
    return rows


async def get_recent_results(db: AsyncSession, account_id: str, keyword: str | None = None) -> list[GroupSearchResult]:
    query = (
        select(GroupSearchResult)
        .where(GroupSearchResult.account_id == account_id)
        .order_by(GroupSearchResult.created_at.desc())
    )
    if keyword:
        query = query.where(GroupSearchResult.keyword == keyword)
    result = await db.execute(query.limit(50))
    return list(result.scalars().all())


async def get_results_by_ids(db: AsyncSession, result_ids: list[str]) -> list[GroupSearchResult]:
    result = await db.execute(select(GroupSearchResult).where(GroupSearchResult.id.in_(result_ids)))
    return list(result.scalars().all())


async def mark_results_joined(db: AsyncSession, result_ids: list[str]) -> None:
    await db.execute(
        select(GroupSearchResult).where(GroupSearchResult.id.in_(result_ids))
        # We just update is_joined
    )
    for rid in result_ids:
        row = await db.get(GroupSearchResult, rid)
        if row:
            row.is_joined = True
    await db.commit()


# --- Join log ---

async def count_today_joins(db: AsyncSession, account_id: str) -> int:
    """Count successful joins made by this account today."""
    today_start = datetime.combine(date.today(), datetime.min.time())
    result = await db.execute(
        select(func.count(GroupJoinLog.id)).where(
            GroupJoinLog.account_id == account_id,
            GroupJoinLog.success == True,  # noqa: E712
            GroupJoinLog.created_at >= today_start,
        )
    )
    return result.scalar() or 0


async def create_join_log(
    db: AsyncSession,
    account_id: str,
    chat_id: str,
    title: str,
    username: str | None,
    keyword: str,
    success: bool,
    error_message: str | None = None,
) -> GroupJoinLog:
    log = GroupJoinLog(
        account_id=account_id,
        chat_id=chat_id,
        title=title,
        username=username,
        keyword=keyword,
        success=success,
        error_message=error_message,
    )
    db.add(log)
    await db.commit()
    await db.refresh(log)
    return log


async def get_join_logs(db: AsyncSession, account_id: str, limit: int = 50) -> list[GroupJoinLog]:
    result = await db.execute(
        select(GroupJoinLog)
        .where(GroupJoinLog.account_id == account_id)
        .order_by(GroupJoinLog.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
