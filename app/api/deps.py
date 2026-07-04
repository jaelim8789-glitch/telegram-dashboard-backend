from dataclasses import dataclass
from typing import Literal

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token, decode_user_id_from_token
from app.crud import api_key as api_key_crud
from app.crud import user as user_crud
from app.database import get_db
from app.models.user import User


@dataclass
class Identity:
    kind: Literal["admin", "api_key", "user"]
    user: User | None = None


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


async def _resolve_identity(x_api_key: str | None, authorization: str | None, db: AsyncSession) -> Identity | None:
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
                return Identity(kind="user", user=user)

    if x_api_key:
        key_row = await api_key_crud.get_by_key(db, x_api_key)
        if key_row is not None and key_row.is_active:
            await api_key_crud.touch_last_used(db, key_row)
            return Identity(kind="api_key")

    return None


async def require_api_key_or_admin(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Guards the main /api/* routes (accounts, broadcast, groups, logs, scheduler,
    telegram-auth, auto-reply). Accepts an admin session, an active API key, or a
    phone-verified user session (app/api/auth.py) — all three are equally trusted to
    operate the dashboard's own Telegram accounts."""
    if await _resolve_identity(x_api_key, authorization, db) is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증이 필요합니다.")


async def get_current_identity(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Identity:
    """Same acceptance rules as require_api_key_or_admin, but returns *which* kind of
    credential authenticated the request — used by GET /api/auth/me so the frontend can
    tell an admin session apart from a regular phone-verified user session."""
    identity = await _resolve_identity(x_api_key, authorization, db)
    if identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증이 필요합니다.")
    return identity
