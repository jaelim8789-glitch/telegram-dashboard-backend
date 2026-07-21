from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Identity
from app.models.account import Account
from app.models.broadcast import Broadcast
from app.models.message_log import MessageLog


@dataclass
class TeleMonMemoryBundle:
    text: str
    top_posts: list[dict]
    periods: dict[str, dict] | None = None


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _truncate_text(text: str, max_chars: int = 120) -> str:
    cleaned = (text or "").strip().replace("\n", " ")
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3] + "..."


async def _resolve_account_ids(db: AsyncSession, identity: Identity) -> list[str]:
    if identity.tenant_id is None:
        return []
    rows = await db.execute(select(Account.id).where(Account.tenant_id == identity.tenant_id))
    return [row[0] for row in rows.all()]


async def _period_summary(db: AsyncSession, account_ids: list[str], start_at: datetime, end_at: datetime | None = None) -> dict:
    if not account_ids:
        return {"attempted": 0, "successful": 0, "success_rate": 0.0}

    conditions = [MessageLog.account_id.in_(account_ids), MessageLog.source == "broadcast", MessageLog.created_at >= start_at]
    if end_at is not None:
        conditions.append(MessageLog.created_at < end_at)

    row = await db.execute(
        select(
            func.count(MessageLog.id),
            func.sum(func.cast(MessageLog.success, func.Integer)),
        ).where(*conditions)
    )
    attempted, successful = row.one()
    attempted_i = int(attempted or 0)
    successful_i = int(successful or 0)
    rate = round((successful_i / attempted_i) * 100, 2) if attempted_i > 0 else 0.0
    return {"attempted": attempted_i, "successful": successful_i, "success_rate": rate}


async def _top_broadcast_posts(db: AsyncSession, account_ids: list[str], *, start_at: datetime, limit: int = 3) -> list[dict]:
    if not account_ids:
        return []

    rows = await db.execute(
        select(
            Broadcast.id,
            Broadcast.message,
            func.count(MessageLog.id).label("attempted"),
            func.sum(func.cast(MessageLog.success, func.Integer)).label("successful"),
            func.max(MessageLog.created_at).label("last_sent_at"),
        )
        .join(
            MessageLog,
            and_(
                MessageLog.source == "broadcast",
                MessageLog.source_id == Broadcast.id,
            ),
        )
        .where(
            MessageLog.account_id.in_(account_ids),
            MessageLog.created_at >= start_at,
        )
        .group_by(Broadcast.id, Broadcast.message)
        .having(func.count(MessageLog.id) >= 3)
        .order_by(
            (func.sum(func.cast(MessageLog.success, func.Integer)) * 1.0 / func.count(MessageLog.id)).desc(),
            func.count(MessageLog.id).desc(),
            func.max(MessageLog.created_at).desc(),
        )
        .limit(limit)
    )

    top_posts: list[dict] = []
    for bid, message, attempted, successful, last_sent_at in rows.all():
        attempted_i = int(attempted or 0)
        successful_i = int(successful or 0)
        rate = round((successful_i / attempted_i) * 100, 2) if attempted_i > 0 else 0.0
        top_posts.append(
            {
                "broadcast_id": str(bid),
                "message_preview": _truncate_text(message, 160),
                "attempted": attempted_i,
                "successful": successful_i,
                "success_rate": rate,
                "last_sent_at": last_sent_at.isoformat() if last_sent_at else None,
            }
        )
    return top_posts


async def build_telemon_memory_context(db: AsyncSession, identity: Identity, user_request: str, *, top_n: int = 3) -> TeleMonMemoryBundle:
    if identity.tenant_id is None:
        return TeleMonMemoryBundle(text="", top_posts=[], periods={})

    account_ids = await _resolve_account_ids(db, identity)
    if not account_ids:
        return TeleMonMemoryBundle(text="", top_posts=[], periods={})

    now = _utcnow_naive()
    this_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_week_start = now - timedelta(days=7)

    this_year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    last_year_start = this_year_start.replace(year=this_year_start.year - 1)

    this_month = await _period_summary(db, account_ids, this_month_start)
    last_week = await _period_summary(db, account_ids, last_week_start)
    last_year = await _period_summary(db, account_ids, last_year_start, this_year_start)
    top_posts = await _top_broadcast_posts(db, account_ids, start_at=now - timedelta(days=180), limit=top_n)

    lines = [
        "[TeleMon 전용 Memory]",
        f"- 이번달 발송: {this_month['attempted']}건, 성공률 {this_month['success_rate']}%",
        f"- 지난주 발송: {last_week['attempted']}건, 성공률 {last_week['success_rate']}%",
        f"- 작년 발송: {last_year['attempted']}건, 성공률 {last_year['success_rate']}%",
    ]

    if top_posts:
        lines.append(f"- 반응 좋았던 글 {len(top_posts)}개 참고:")
        for idx, post in enumerate(top_posts, start=1):
            lines.append(
                f"  {idx}) 성공률 {post['success_rate']}% / 시도 {post['attempted']}건 / 문구: {post['message_preview']}"
            )
    else:
        lines.append("- 반응 좋았던 글 데이터가 아직 부족함(최근 180일 기준 최소 시도 3건 필요)")

    lower_req = (user_request or "").lower()
    if any(token in lower_req for token in ("홍보", "프로모션", "다시", "이번에도", "재사용")) and top_posts:
        lines.append("- 지시: 사용자가 재홍보를 요청하면 위 고성과 글 3개를 참고했다고 먼저 밝힌 뒤 개선안을 제시할 것")

    return TeleMonMemoryBundle(text="\n".join(lines), top_posts=top_posts)


async def build_telemon_memory_snapshot(db: AsyncSession, identity: Identity) -> dict:
    bundle = await build_telemon_memory_context(db, identity, "memory snapshot")
    if identity.tenant_id is None:
        return {"generated_at": _utcnow_naive().isoformat(), "periods": {}, "top_posts": [], "memory_text": ""}

    account_ids = await _resolve_account_ids(db, identity)
    now = _utcnow_naive()
    this_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_week_start = now - timedelta(days=7)
    this_year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    last_year_start = this_year_start.replace(year=this_year_start.year - 1)

    periods = {
        "this_month": await _period_summary(db, account_ids, this_month_start),
        "last_week": await _period_summary(db, account_ids, last_week_start),
        "last_year": await _period_summary(db, account_ids, last_year_start, this_year_start),
    }
    return {
        "generated_at": now.isoformat(),
        "periods": periods,
        "top_posts": bundle.top_posts,
        "memory_text": bundle.text,
    }