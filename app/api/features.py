"""All-in-one API for: Message Templates, Follow-up Rules, Team Members, Usage Dashboard, Calendar."""

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database import get_db, async_session_maker
from app.models.account import Account
from app.models.message_template import FollowUpRule, MessageTemplate, TeamMember
from app.models.tenant import UsageRecord

router = APIRouter(prefix="/api/features", tags=["features"])
logger = get_logger(__name__)


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ═══════════════════════════════════════════════════════════════════
# 1. 📋 MESSAGE TEMPLATES
# ═══════════════════════════════════════════════════════════════════

@router.get("/{tenant_id}/templates")
async def list_templates(tenant_id: str, category: str | None = None, db: AsyncSession = Depends(get_db)):
    query = select(MessageTemplate).where(MessageTemplate.tenant_id == tenant_id).order_by(MessageTemplate.use_count.desc())
    if category:
        query = query.where(MessageTemplate.category == category)
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/{tenant_id}/templates")
async def create_template(tenant_id: str, data: dict, db: AsyncSession = Depends(get_db)):
    tmpl = MessageTemplate(
        tenant_id=tenant_id,
        name=data["name"],
        category=data.get("category", "general"),
        content=data["content"],
        variables=json.dumps(data.get("variables", [])),
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    logger.info("template_created", tenant_id=tenant_id, template_id=tmpl.id)
    return tmpl


@router.put("/{tenant_id}/templates/{template_id}")
async def update_template(tenant_id: str, template_id: str, data: dict, db: AsyncSession = Depends(get_db)):
    tmpl = await db.get(MessageTemplate, template_id)
    if not tmpl or tmpl.tenant_id != tenant_id:
        raise HTTPException(404, "템플릿을 찾을 수 없습니다.")
    if "name" in data: tmpl.name = data["name"]
    if "category" in data: tmpl.category = data["category"]
    if "content" in data: tmpl.content = data["content"]
    if "variables" in data: tmpl.variables = json.dumps(data["variables"])
    if "is_favorite" in data: tmpl.is_favorite = data["is_favorite"]
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


@router.delete("/{tenant_id}/templates/{template_id}")
async def delete_template(tenant_id: str, template_id: str, db: AsyncSession = Depends(get_db)):
    tmpl = await db.get(MessageTemplate, template_id)
    if not tmpl or tmpl.tenant_id != tenant_id:
        raise HTTPException(404, "템플릿을 찾을 수 없습니다.")
    await db.delete(tmpl)
    await db.commit()
    return {"status": "deleted"}


@router.post("/{tenant_id}/templates/{template_id}/use")
async def increment_template_use(tenant_id: str, template_id: str, db: AsyncSession = Depends(get_db)):
    tmpl = await db.get(MessageTemplate, template_id)
    if tmpl and tmpl.tenant_id == tenant_id:
        tmpl.use_count = (tmpl.use_count or 0) + 1
        await db.commit()
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════
# 2. 🔄 FOLLOW-UP RULES (후속 메시지)
# ═══════════════════════════════════════════════════════════════════

@router.get("/{tenant_id}/follow-ups")
async def list_follow_ups(tenant_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(FollowUpRule).where(FollowUpRule.tenant_id == tenant_id).order_by(FollowUpRule.created_at.desc())
    )
    return result.scalars().all()


@router.post("/{tenant_id}/follow-ups")
async def create_follow_up(tenant_id: str, data: dict, db: AsyncSession = Depends(get_db)):
    rule = FollowUpRule(
        tenant_id=tenant_id,
        account_id=data["account_id"],
        name=data["name"],
        message_content=data["message_content"],
        trigger_delay_hours=data.get("trigger_delay_hours", 24),
        match_keyword=data.get("match_keyword"),
        max_sends_per_day=data.get("max_sends_per_day", 50),
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    logger.info("follow_up_created", tenant_id=tenant_id, rule_id=rule.id)
    return rule


@router.put("/{tenant_id}/follow-ups/{rule_id}")
async def update_follow_up(tenant_id: str, rule_id: str, data: dict, db: AsyncSession = Depends(get_db)):
    rule = await db.get(FollowUpRule, rule_id)
    if not rule or rule.tenant_id != tenant_id:
        raise HTTPException(404, "규칙을 찾을 수 없습니다.")
    for field in ["name", "message_content", "trigger_delay_hours", "match_keyword", "max_sends_per_day", "is_active"]:
        if field in data:
            setattr(rule, field, data[field])
    await db.commit()
    await db.refresh(rule)
    return rule


@router.delete("/{tenant_id}/follow-ups/{rule_id}")
async def delete_follow_up(tenant_id: str, rule_id: str, db: AsyncSession = Depends(get_db)):
    rule = await db.get(FollowUpRule, rule_id)
    if not rule or rule.tenant_id != tenant_id:
        raise HTTPException(404, "규칙을 찾을 수 없습니다.")
    await db.delete(rule)
    await db.commit()
    return {"status": "deleted"}


# ═══════════════════════════════════════════════════════════════════
# 3. 👥 TEAM MEMBERS
# ═══════════════════════════════════════════════════════════════════

@router.get("/{tenant_id}/team")
async def list_team_members(tenant_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(TeamMember).where(TeamMember.tenant_id == tenant_id).order_by(TeamMember.created_at.asc())
    )
    return result.scalars().all()


@router.post("/{tenant_id}/team")
async def add_team_member(tenant_id: str, data: dict, db: AsyncSession = Depends(get_db)):
    member = TeamMember(
        tenant_id=tenant_id,
        username=data["username"],
        role=data.get("role", "operator"),
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)
    logger.info("team_member_added", tenant_id=tenant_id, member_id=member.id)
    return member


@router.put("/{tenant_id}/team/{member_id}")
async def update_team_member(tenant_id: str, member_id: str, data: dict, db: AsyncSession = Depends(get_db)):
    member = await db.get(TeamMember, member_id)
    if not member or member.tenant_id != tenant_id:
        raise HTTPException(404, "팀원을 찾을 수 없습니다.")
    if "role" in data: member.role = data["role"]
    if "is_active" in data: member.is_active = data["is_active"]
    await db.commit()
    await db.refresh(member)
    return member


@router.delete("/{tenant_id}/team/{member_id}")
async def remove_team_member(tenant_id: str, member_id: str, db: AsyncSession = Depends(get_db)):
    member = await db.get(TeamMember, member_id)
    if not member or member.tenant_id != tenant_id:
        raise HTTPException(404, "팀원을 찾을 수 없습니다.")
    await db.delete(member)
    await db.commit()
    return {"status": "deleted"}


# ═══════════════════════════════════════════════════════════════════
# 4. 📊 USAGE DASHBOARD
# ═══════════════════════════════════════════════════════════════════

@router.get("/{tenant_id}/dashboard")
async def get_usage_dashboard(tenant_id: str, days: int = Query(default=30, le=90)):
    """Get usage statistics for dashboard charts."""
    now = utcnow_naive()
    start = now - timedelta(days=days)

    async with async_session_maker() as db:
        # Daily usage
        result = await db.execute(
            select(
                func.date(UsageRecord.recorded_at).label("date"),
                UsageRecord.action,
                func.sum(UsageRecord.count).label("total"),
            ).where(
                UsageRecord.tenant_id == tenant_id,
                UsageRecord.recorded_at >= start,
            ).group_by("date", UsageRecord.action).order_by("date")
        )
        rows = result.all()

    # Build chart data
    daily_data = {}
    for row in rows:
        date_str = str(row.date)
        if date_str not in daily_data:
            daily_data[date_str] = {"date": date_str, "broadcast": 0, "auto_reply": 0, "reply_macro": 0}
        action_key = row.action if row.action in daily_data[date_str] else "broadcast"
        daily_data[date_str][action_key] = (daily_data[date_str].get(action_key, 0) or 0) + (row.total or 0)

    # Totals
    total_broadcast = sum(d.get("broadcast", 0) for d in daily_data.values())
    total_auto_reply = sum(d.get("auto_reply", 0) for d in daily_data.values())
    total_macro = sum(d.get("reply_macro", 0) for d in daily_data.values())

    return {
        "daily": sorted(daily_data.values(), key=lambda x: x["date"]),
        "totals": {
            "broadcast": total_broadcast,
            "auto_reply": total_auto_reply,
            "reply_macro": total_macro,
            "all": total_broadcast + total_auto_reply + total_macro,
        },
        "period_days": days,
    }


# ═══════════════════════════════════════════════════════════════════
# 5. 📅 SCHEDULE CALENDAR
# ═══════════════════════════════════════════════════════════════════

@router.get("/{tenant_id}/calendar")
async def get_calendar_data(tenant_id: str, year: int | None = None, month: int | None = None):
    """Get scheduled broadcasts grouped by date for calendar view."""
    from app.models.broadcast import Broadcast

    now = utcnow_naive()
    y = year or now.year
    m = month or now.month

    month_start = datetime(y, m, 1)
    if m == 12:
        month_end = datetime(y + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = datetime(y, m + 1, 1) - timedelta(days=1)
    month_end = month_end.replace(hour=23, minute=59, second=59)

    async with async_session_maker() as db:
        account_ids_result = await db.execute(
            select(Account.id).where(Account.tenant_id == tenant_id)
        )
        account_ids = [row[0] for row in account_ids_result.all()]

        if not account_ids:
            return {"year": y, "month": m, "days": []}

        result = await db.execute(
            select(
                func.date(Broadcast.scheduled_at).label("date"),
                func.count().label("count"),
                func.sum(func.cast(Broadcast.status == "pending", func.INTEGER)).label("pending"),
                func.sum(func.cast(Broadcast.status == "sent", func.INTEGER)).label("sent"),
                func.sum(func.cast(Broadcast.status == "failed", func.INTEGER)).label("failed"),
            ).where(
                Broadcast.account_id.in_(account_ids),
                Broadcast.scheduled_at >= month_start,
                Broadcast.scheduled_at <= month_end,
            ).group_by("date").order_by("date")
        )
        rows = result.all()

    return {
        "year": y,
        "month": m,
        "days": [
            {
                "date": str(row.date),
                "total": row.count,
                "pending": row.pending or 0,
                "sent": row.sent or 0,
                "failed": row.failed or 0,
            }
            for row in rows
        ],
    }