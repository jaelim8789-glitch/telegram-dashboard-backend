from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.crud import account as account_crud
from app.database import get_db
from app.schemas.account import AccountCreate, AccountRead, AccountUpdate

router = APIRouter(prefix="/api/accounts", tags=["accounts"])
logger = get_logger(__name__)


@router.get("", response_model=list[AccountRead])
async def read_accounts(db: AsyncSession = Depends(get_db)):
    return await account_crud.list_accounts(db)


@router.post("", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
async def create_account(payload: AccountCreate, db: AsyncSession = Depends(get_db)):
    try:
        account = await account_crud.create_account(db, payload)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 등록된 전화번호입니다.")
    logger.info("account_created", account_id=account.id)
    return account


@router.get("/{account_id}", response_model=AccountRead)
async def read_account(account_id: str, db: AsyncSession = Depends(get_db)):
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    return account


@router.put("/{account_id}", response_model=AccountRead)
async def update_account(account_id: str, payload: AccountUpdate, db: AsyncSession = Depends(get_db)):
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    return await account_crud.update_account(db, account, payload)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(account_id: str, db: AsyncSession = Depends(get_db)):
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    await account_crud.delete_account(db, account)
    logger.info("account_deleted", account_id=account_id)
