from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Identity, get_current_identity
from app.core.logging import get_logger
from app.database import get_db
from app.models.account import Account
from app.models.broadcast import Broadcast
from app.models.message_log import MessageLog
from app.models.tenant import Tenant
from app.models.user import User

router = APIRouter(prefix="/api/users", tags=["users"])
logger = get_logger(__name__)


@router.delete("/me")
async def delete_my_account(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    if identity.kind != "user" or identity.user is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="사용자 계정만 삭제할 수 있습니다.")

    user = identity.user
    phone = user.phone
    user_id = user.id

    from sqlalchemy import select as sa_select

    tenant_result = await db.execute(sa_select(Tenant).where(Tenant.phone == phone))
    tenant = tenant_result.scalar_one_or_none()

    account_ids = []
    if tenant:
        account_result = await db.execute(
            sa_select(Account.id).where(Account.tenant_id == tenant.id)
        )
        account_ids = [row[0] for row in account_result.all()]

    if account_ids:
        await db.execute(delete(MessageLog).where(MessageLog.account_id.in_(account_ids)))
        await db.execute(delete(Broadcast).where(Broadcast.account_id.in_(account_ids)))
        await db.execute(delete(Account).where(Account.tenant_id == tenant.id))

        from app.models.api_key import APIKey
        await db.execute(delete(APIKey).where(APIKey.tenant_id == tenant.id))

    if tenant:
        await db.delete(tenant)

    await db.delete(user)
    await db.commit()

    logger.info("account_deleted", user_id=user_id, phone=phone)
    return {"message": "계정이 삭제되었습니다"}
