from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.database import get_db
from app.models.account import Account
from app.schemas.account import AccountCreate, AccountRead, AccountUpdate

router = APIRouter(prefix="/api/accounts", tags=["accounts"])
logger = get_logger(__name__)


@router.get("", response_model=list[AccountRead])
async def read_accounts(db: AsyncSession = Depends(get_db), identity: Identity = Depends(get_current_identity)):
    if identity.kind == "admin":
        return await account_crud.list_accounts(db)
    
    if identity.tenant_id:
        result = await db.execute(
            select(Account).where(Account.tenant_id == identity.tenant_id).order_by(Account.created_at.desc())
        )
        return list(result.scalars().all())
    
    # API keys without tenant context see no accounts (fail closed)
    return []


@router.post("", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
async def create_account(payload: AccountCreate, db: AsyncSession = Depends(get_db), identity: Identity = Depends(get_current_identity)):
    try:
        account_data = payload.model_dump()
        if identity.tenant_id:
            account_data["tenant_id"] = identity.tenant_id
        account = await account_crud.create_account(db, AccountCreate(**account_data) if hasattr(payload, 'model_dump') else payload)
        if identity.tenant_id:
            account.tenant_id = identity.tenant_id
            await db.commit()
            await db.refresh(account)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 등록된 전화번호입니다.")
    logger.info("account_created", account_id=account.id)
    return account


@router.get("/{account_id}", response_model=AccountRead)
async def read_account(account_id: str, db: AsyncSession = Depends(get_db), identity: Identity = Depends(get_current_identity)):
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    return account


@router.put("/{account_id}", response_model=AccountRead)
async def update_account(account_id: str, payload: AccountUpdate, db: AsyncSession = Depends(get_db), identity: Identity = Depends(get_current_identity)):
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    return await account_crud.update_account(db, account, payload)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(account_id: str, db: AsyncSession = Depends(get_db), identity: Identity = Depends(get_current_identity)):
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    await account_crud.delete_account(db, account)
    logger.info("account_deleted", account_id=account_id)