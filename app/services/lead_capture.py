"""CRM / 리드 생성 서비스.
    
Captures lead information when auto-reply or reply-macro interacts with users.
This enables sales teams to follow up with potential customers.
"""

import json
from datetime import datetime, timezone

from app.core.logging import get_logger
from app.crud import account as account_crud
from app.database import async_session_maker
from app.models.tenant import Lead

logger = get_logger(__name__)


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def capture_lead(
    tenant_id: str,
    account_id: str,
    telegram_user_id: str,
    telegram_username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    source_chat_id: str = "",
    source_rule_id: str | None = None,
    message_text: str = "",
) -> Lead | None:
    """Capture or update a lead from an auto-reply / macro interaction."""
    async with async_session_maker() as db:
        # Check if lead already exists
        from sqlalchemy import select
        result = await db.execute(
            select(Lead).where(
                Lead.tenant_id == tenant_id,
                Lead.telegram_user_id == telegram_user_id,
            )
        )
        lead = result.scalar_one_or_none()

        now = utcnow_naive()

        if lead:
            # Update existing lead
            lead.total_messages = (lead.total_messages or 0) + 1
            lead.last_interaction = now
            if telegram_username:
                lead.telegram_username = telegram_username
            if first_name:
                lead.first_name = first_name
            if last_name:
                lead.last_name = last_name
        else:
            # Create new lead
            lead = Lead(
                tenant_id=tenant_id,
                account_id=account_id,
                telegram_user_id=telegram_user_id,
                telegram_username=telegram_username,
                first_name=first_name,
                last_name=last_name,
                source_chat_id=source_chat_id,
                source_rule_id=source_rule_id,
                total_messages=1,
                last_interaction=now,
            )
            db.add(lead)

        await db.commit()
        await db.refresh(lead)

        logger.info(
            "lead_captured",
            tenant_id=tenant_id,
            telegram_user_id=telegram_user_id,
            is_new=lead.total_messages == 1,
        )
        return lead


async def get_leads(tenant_id: str, limit: int = 50, offset: int = 0) -> list[Lead]:
    """Get leads for a tenant with pagination."""
    from sqlalchemy import desc, select

    async with async_session_maker() as db:
        result = await db.execute(
            select(Lead)
            .where(Lead.tenant_id == tenant_id)
            .order_by(desc(Lead.last_interaction))
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())


async def get_lead_count(tenant_id: str) -> int:
    """Get total lead count for a tenant."""
    from sqlalchemy import func, select

    async with async_session_maker() as db:
        result = await db.execute(
            select(func.count()).select_from(Lead).where(Lead.tenant_id == tenant_id)
        )
        return result.scalar_one()


async def export_leads_csv(tenant_id: str) -> str:
    """Export leads as CSV string."""
    leads = await get_leads(tenant_id, limit=9999)
    lines = ["telegram_user_id,username,first_name,last_name,source_chat_id,total_messages,last_interaction,created_at"]
    for lead in leads:
        lines.append(
            f"{lead.telegram_user_id},{lead.telegram_username or ''},{lead.first_name or ''},"
            f"{lead.last_name or ''},{lead.source_chat_id},{lead.total_messages or 0},"
            f"{lead.last_interaction or ''},{lead.created_at or ''}"
        )
    return "\n".join(lines)