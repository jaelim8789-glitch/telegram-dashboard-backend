from dataclasses import dataclass
from typing import Literal

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token, decode_user_id_from_token
from app.crud import api_key as api_key_crud
from app.crud import session as session_crud
from app.crud import user as user_crud
from app.database import get_db
from app.models.user import User


@dataclass
class Identity:
    kind: Literal["admin", "api_key", "user"]
    user: User | None = None
    tenant_id: str | None = None


async def get_current_identity(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
    db: AsyncSession = Depends(get_db),
) -> Identity:
    """Returns a fully-resolved Identity including tenant_id."""
    identity = await _resolve_identity(x_api_key, authorization, x_session_token, db)
    if identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증이 필요합니다.")
    return identity


async def get_current_tenant_id(
    identity: Identity = Depends(get_current_identity),
) -> str:
    """FastAPI dependency that extracts the current tenant_id from the auth context.
    
    Returns the tenant_id string. Raises 401 if not authenticated.
    Used by AI Platform routers and other tenant-scoped endpoints.
    """
    if identity.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="테넌트 정보가 없습니다. API 키 또는 세션으로 인증해주세요.",
        )
    return identity.tenant_id


async def require_admin(authorization: str | None = Header(default=None)) -> None:
    """Guards /api/admin/* — a valid admin JWT only, no X-API-Key or user-session
    alternative (API keys and users are themselves managed here, so neither can also
    unlock managing the other)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="관리자 로그인이 필요합니다.")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        if not decode_access_token(token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 토큰입니다.")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않거나 만료된 토큰입니다.")


async def _resolve_identity(x_api_key: str | None, authorization: str | None, x_session_token: str | None, db: AsyncSession) -> Identity | None:
    # Session token (opaque persistent token, survives browser restart)
    if x_session_token:
        session = await session_crud.get_session_by_token(db, x_session_token)
        if session is not None:
            await session_crud.touch_session(db, session)
            return Identity(kind="user", tenant_id=session.tenant_id)

    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        try:
            if decode_access_token(token):
                return Identity(kind="admin")
        except jwt.PyJWTError:
            pass
        try:
            user_id = decode_user_id_from_token(token)
        except jwt.PyJWTError:
            user_id = None
        if user_id:
            user = await user_crud.get_user(db, user_id)
            if user is not None and user.is_active:
                # Resolve tenant_id from the user's phone to Tenant.phone
                tenant_id = await _resolve_tenant_by_phone(db, user.phone)
                return Identity(kind="user", user=user, tenant_id=tenant_id)
            # Bearer token might be an API key JWT (sub="user:{api_key_id}")
            key_row = await api_key_crud.get_api_key(db, user_id)
            if key_row is not None and key_row.is_active:
                await api_key_crud.touch_last_used(db, key_row)
                return Identity(kind="api_key", tenant_id=key_row.tenant_id)

    if x_api_key:
        key_row = await api_key_crud.get_by_key(db, x_api_key)
        if key_row is not None and key_row.is_active:
            await api_key_crud.touch_last_used(db, key_row)
            tenant_id = key_row.tenant_id
            return Identity(kind="api_key", tenant_id=tenant_id)

    return None


async def _resolve_tenant_by_phone(db: AsyncSession, phone: str) -> str | None:
    """Resolve tenant_id by matching phone with Tenant.phone."""
    from sqlalchemy import select
    from app.models.tenant import Tenant
    result = await db.execute(select(Tenant.id).where(Tenant.phone == phone))
    return result.scalar_one_or_none()


async def require_api_key_or_admin(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Guards the main /api/* routes."""
    if await _resolve_identity(x_api_key, authorization, x_session_token, db) is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증이 필요합니다.")


async def require_tenant_access(
    tenant_id: str,
    identity: Identity = Depends(get_current_identity),
) -> None:
    """Reusable dependency: verify the authenticated identity owns this tenant.
    
    Admin can access any tenant. API key and user must match the tenant.
    Call as: Depends(require_tenant_access) with the tenant_id from the path.
    """
    if identity.kind == "admin":
        return  # admin is cross-tenant
    
    if identity.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="이 기능에 접근할 수 없습니다. 먼저 결제/요금제를 설정해주세요.",
        )
    
    if identity.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="다른 테넌트의 리소스에 접근할 수 없습니다.",
        )


async def require_account_tenant_access(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
) -> str:
    """Verify the authenticated identity's tenant owns the given Account.
    
    Returns the tenant_id if authorized. Raises 403/404 otherwise.
    """
    from app.crud import account as account_crud
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    
    if identity.kind == "admin":
        return account.tenant_id or ""
    
    if identity.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="이 기능에 접근할 수 없습니다. 먼저 결제/요금제를 설정해주세요.",
        )
    
    if account.tenant_id != identity.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="해당 계정에 접근할 수 없습니다.",
        )

    return identity.tenant_id


async def require_account_capacity(
    db: AsyncSession,
    identity: Identity,
) -> None:
    """Enforce the tenant's max_accounts plan limit before a new Account is created.

    Admin and identities with no resolved tenant (legacy/ungated accounts, same
    carve-out as require_tenant_access) are not capacity-checked.
    """
    if identity.kind == "admin" or identity.tenant_id is None:
        return

    from sqlalchemy import func, select

    from app.models.account import Account
    from app.models.tenant import Tenant

    tenant = await db.get(Tenant, identity.tenant_id)
    if tenant is None:
        return

    result = await db.execute(
        select(func.count()).select_from(Account).where(Account.tenant_id == identity.tenant_id)
    )
    current_count = result.scalar_one()
    if current_count >= tenant.max_accounts:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"현재 요금제의 계정 한도({tenant.max_accounts}개)에 도달했습니다. 요금제를 업그레이드해주세요.",
        )


async def require_broadcast_capacity(
    db: AsyncSession,
    identity: Identity,
) -> None:
    """Enforce the tenant's can_broadcast flag and monthly message limit before a
    Broadcast is created. Same admin/no-tenant carve-out as require_account_capacity.
    """
    if identity.kind == "admin" or identity.tenant_id is None:
        return

    from app.models.tenant import Tenant
    from app.services.usage_tracker import check_usage_limit

    tenant = await db.get(Tenant, identity.tenant_id)
    if tenant is None:
        return

    if not tenant.can_broadcast:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="현재 요금제에서는 발송 기능을 사용할 수 없습니다. 요금제를 업그레이드해주세요.",
        )

    if not await check_usage_limit(db, tenant, "broadcast"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="이번 달 발송 한도에 도달했습니다. 요금제를 업그레이드해주세요.",
        )