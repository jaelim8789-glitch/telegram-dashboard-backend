from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import account as account_crud
from app.database import get_db
from app.schemas.group import GroupRead
from app.services.telegram_actions import AccountNotAuthenticatedError, list_groups

router = APIRouter(prefix="/api/accounts", tags=["groups"])


@router.get("/{account_id}/groups", response_model=list[GroupRead])
async def read_groups(account_id: str, db: AsyncSession = Depends(get_db)):
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    try:
        return await list_groups(account)
    except AccountNotAuthenticatedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
