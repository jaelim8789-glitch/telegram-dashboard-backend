from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.schemas.account import (
    AccountCreate,
    AccountFilterParams,
    AccountSortParams,
    AccountUpdate,
)


async def list_accounts(db: AsyncSession) -> list[Account]:
    result = await db.execute(select(Account).order_by(Account.created_at.desc()))
    return list(result.scalars().all())


async def get_account(db: AsyncSession, account_id: str) -> Account | None:
    return await db.get(Account, account_id)


async def get_account_by_phone(db: AsyncSession, phone: str) -> Account | None:
    result = await db.execute(select(Account).where(Account.phone == phone))
    return result.scalar_one_or_none()


async def create_account(db: AsyncSession, data: AccountCreate) -> Account:
    account = Account(phone=data.phone, name=data.name)
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


async def update_account(db: AsyncSession, account: Account, data: AccountUpdate) -> Account:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(account, field, value)
    await db.commit()
    await db.refresh(account)
    return account


async def delete_account(db: AsyncSession, account: Account) -> None:
    await db.delete(account)
    await db.commit()


async def set_auto_reply_enabled(db: AsyncSession, account: Account, enabled: bool) -> Account:
    account.auto_reply_enabled = enabled
    await db.commit()
    await db.refresh(account)
    return account


async def set_auth_state(
    db: AsyncSession,
    account: Account,
    *,
    status: str,
    session_data: str | None = None,
    touch_activity: bool = True,
) -> Account:
    account.status = status
    if session_data is not None:
        account.session_data = session_data
    if touch_activity:
        account.last_activity = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    await db.refresh(account)
    return account


async def save_session_snapshot(db: AsyncSession, account: Account, session_data: str) -> Account:
    """Persist the current Telethon session string without touching auth status.

    Called after every step of the multi-step login flow (send-code, verify-code,
    verify-2fa) so a mid-flow backend restart doesn't strand the account: the next
    request rebuilds the pool's TelegramClient from this session string instead of
    a blank one, preserving the auth_key already negotiated with Telegram's DC —
    even if the user hasn't finished 2FA yet.
    """
    account.session_data = session_data
    await db.commit()
    await db.refresh(account)
    return account


async def mark_account_session_invalid(db: AsyncSession, account: Account) -> Account:
    account.session_data = None
    account.status = "inactive"
    account.last_activity = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    await db.refresh(account)
    return account


async def mark_account_banned(db: AsyncSession, account: Account) -> Account:
    account.status = "banned"
    account.session_data = None
    account.last_activity = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    await db.refresh(account)
    return account


async def clear_account_error(db: AsyncSession, account: Account) -> Account:
    """User acknowledged/dismissed the last error — clear it so the health
    badge stops showing it until a new failure actually occurs."""
    account.last_error = None
    account.last_error_at = None
    await db.commit()
    await db.refresh(account)
    return account


# ── Search / Filter / Sort / Paginate ────────────────────────────────────


async def query_accounts(
    db: AsyncSession,
    tenant_id: str | None = None,
    filters: AccountFilterParams | None = None,
    sort: AccountSortParams | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Account], int]:
    query = select(Account)
    count_query = select(Account.id)

    if tenant_id:
        query = query.where(Account.tenant_id == tenant_id)
        count_query = count_query.where(Account.tenant_id == tenant_id)

    if filters:
        if filters.search:
            pattern = f"%{filters.search}%"
            query = query.where(
                or_(Account.phone.ilike(pattern), Account.name.ilike(pattern))
            )
            count_query = count_query.where(
                or_(Account.phone.ilike(pattern), Account.name.ilike(pattern))
            )
        if filters.status:
            query = query.where(Account.status == filters.status)
            count_query = count_query.where(Account.status == filters.status)
        if filters.has_session is not None:
            if filters.has_session:
                query = query.where(Account.session_data.isnot(None))
                count_query = count_query.where(Account.session_data.isnot(None))
            else:
                query = query.where(Account.session_data.is_(None))
                count_query = count_query.where(Account.session_data.is_(None))
        if filters.has_error is not None:
            if filters.has_error:
                query = query.where(Account.last_error.isnot(None))
                count_query = count_query.where(Account.last_error.isnot(None))
            else:
                query = query.where(Account.last_error.is_(None))
                count_query = count_query.where(Account.last_error.is_(None))
        if filters.auto_reply_enabled is not None:
            query = query.where(Account.auto_reply_enabled == filters.auto_reply_enabled)
            count_query = count_query.where(Account.auto_reply_enabled == filters.auto_reply_enabled)
        if filters.phone:
            query = query.where(Account.phone.ilike(f"%{filters.phone}%"))
            count_query = count_query.where(Account.phone.ilike(f"%{filters.phone}%"))

    # Count total before pagination
    count_result = await db.execute(select(func.count()).select_from(count_query.subquery()))
    total = count_result.scalar() or 0

    # Sort
    sort_field_map = {
        "created_at": Account.created_at,
        "updated_at": Account.updated_at,
        "phone": Account.phone,
        "name": Account.name,
        "status": Account.status,
        "today_sent": Account.today_sent,
        "group_count": Account.group_count,
        "last_activity": Account.last_activity,
        "last_error_at": Account.last_error_at,
        "last_success_at": Account.last_success_at,
    }
    sort_col = sort_field_map.get(sort.sort_by if sort else "created_at", Account.created_at)
    if sort and sort.sort_dir == "asc":
        query = query.order_by(sort_col.asc())
    else:
        query = query.order_by(sort_col.desc())

    # Paginate
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)

    result = await db.execute(query)
    accounts = list(result.scalars().all())

    return accounts, total


# ── Health field updates ─────────────────────────────────────────────────


async def update_account_health(
    db: AsyncSession,
    account: Account,
    *,
    last_error: str | None = None,
    last_error_at: datetime | None = None,
    last_success_at: datetime | None = None,
    health_checked_at: datetime | None = None,
) -> Account:
    if last_error is not None:
        account.last_error = last_error
    if last_error_at is not None:
        account.last_error_at = last_error_at
    if last_success_at is not None:
        account.last_success_at = last_success_at
    if health_checked_at is not None:
        account.health_checked_at = health_checked_at
    await db.commit()
    await db.refresh(account)
    return account


# ── Bulk Operations ──────────────────────────────────────────────────────


async def bulk_activate_accounts(db: AsyncSession, account_ids: list[str]) -> int:
    from sqlalchemy import update
    result = await db.execute(
        update(Account)
        .where(Account.id.in_(account_ids))
        .values(status="active")
    )
    await db.commit()
    return result.rowcount


async def bulk_deactivate_accounts(db: AsyncSession, account_ids: list[str]) -> int:
    from sqlalchemy import update
    result = await db.execute(
        update(Account)
        .where(Account.id.in_(account_ids))
        .values(status="inactive")
    )
    await db.commit()
    return result.rowcount


async def bulk_reset_sessions(db: AsyncSession, account_ids: list[str]) -> int:
    from sqlalchemy import update
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result = await db.execute(
        update(Account)
        .where(Account.id.in_(account_ids))
        .values(session_data=None, status="inactive", last_activity=now)
    )
    await db.commit()
    return result.rowcount


async def bulk_delete_accounts(db: AsyncSession, account_ids: list[str]) -> int:
    from sqlalchemy import delete
    result = await db.execute(
        delete(Account).where(Account.id.in_(account_ids))
    )
    await db.commit()
    return result.rowcount


# ── Summary ──────────────────────────────────────────────────────────────


async def get_account_summary(db: AsyncSession, tenant_id: str | None = None) -> dict:
    from sqlalchemy import case

    base_query = select(
        func.count(Account.id).label("total"),
        func.sum(case((Account.status == "active", 1), else_=0)).label("active"),
        func.sum(case((Account.status == "inactive", 1), else_=0)).label("inactive"),
        func.sum(case((Account.status == "banned", 1), else_=0)).label("banned"),
        func.sum(case((Account.session_data.isnot(None), 1), else_=0)).label("has_session"),
        func.sum(case((Account.last_error.isnot(None), 1), else_=0)).label("has_errors"),
        func.coalesce(func.sum(Account.today_sent), 0).label("total_sent"),
        func.coalesce(func.sum(Account.group_count), 0).label("total_groups"),
    )
    if tenant_id:
        base_query = base_query.where(Account.tenant_id == tenant_id)

    row = (await db.execute(base_query)).one()

    return {
        "total": row.total or 0,
        "active_accounts": row.active or 0,
        "inactive_accounts": row.inactive or 0,
        "banned": row.banned or 0,
        "has_session": row.has_session or 0,
        "has_errors": row.has_errors or 0,
        "total_today_sent": row.total_sent or 0,
        "total_groups": row.total_groups or 0,
    }
