import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.crud import api_key as api_key_crud
from app.database import get_db


async def require_admin(authorization: str | None = Header(default=None)) -> None:
    """Guards /api/admin/* — a valid admin JWT only, no X-API-Key alternative (API keys
    are themselves managed here, so they can't also unlock managing them)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="관리자 로그인이 필요합니다.")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        if not decode_access_token(token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 토큰입니다.")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않거나 만료된 토큰입니다.")


async def require_api_key_or_admin(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Guards the main /api/* routes (accounts, broadcast, groups, logs, scheduler,
    telegram-auth). Accepts either an active API key (for external/programmatic access)
    or a valid admin session (so the logged-in dashboard itself doesn't need to separately
    provision and carry an API key just to talk to its own backend)."""
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        try:
            if decode_access_token(token):
                return
        except jwt.PyJWTError:
            pass

    if x_api_key:
        key_row = await api_key_crud.get_by_key(db, x_api_key)
        if key_row is not None and key_row.is_active:
            await api_key_crud.touch_last_used(db, key_row)
            return

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증이 필요합니다.")
