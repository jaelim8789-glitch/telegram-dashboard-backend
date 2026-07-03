from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.schemas.account import AccountCreate, AccountUpdate


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
