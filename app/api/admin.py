from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.config import settings
from app.core.logging import get_logger
from app.core.rate_limiter import check_rate_limit, get_retry_after_seconds
from app.core.security import create_access_token, generate_user_api_key, hash_api_key, mask_api_key, verify_admin_credentials
from app.crud import api_key as api_key_crud
from app.crud import user as user_crud
from app.database import get_db
from app.schemas.admin import AdminLoginRequest, AdminMeResponse, AdminTokenResponse
from app.schemas.api_key import APIKeyCreated, APIKeyCreateRequest, APIKeyRead
from app.schemas.user import UserApiKeyReissued, UserRead, UserToggleRequest

router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = get_logger(__name__)


@router.post("/login", response_model=AdminTokenResponse)
async def login(payload: AdminLoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(client_ip, "admin_login", max_attempts=10, window_seconds=300):
        retry_after = get_retry_after_seconds(client_ip, "admin_login")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="너무 많은 로그인 시도가 있었습니다. 잠시 후 다시 시도해주세요.",
            headers={"Retry-After": str(retry_after)},
        )
    if not verify_admin_credentials(payload.username, payload.password):
        logger.warning("admin_login_failed", username=payload.username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="아이디 또는 비밀번호가 올바르지 않습니다.")
    logger.info("admin_login_success")
    return AdminTokenResponse(access_token=create_access_token())


@router.get("/me", response_model=AdminMeResponse, dependencies=[Depends(require_admin)])
async def me():
    return AdminMeResponse(username=settings.admin_username)


@router.post(
    "/api-keys",
    response_model=APIKeyCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_api_key(payload: APIKeyCreateRequest, db: AsyncSession = Depends(get_db)):
    api_key = await api_key_crud.create_api_key(db, payload.name, tenant_id=payload.tenant_id)
    logger.info("api_key_created", api_key_id=api_key.id, name=api_key.name, tenant_id=payload.tenant_id)
    return APIKeyCreated(id=api_key.id, key=api_key.key, name=api_key.name, created_at=api_key.created_at)


@router.get("/api-keys", response_model=list[APIKeyRead], dependencies=[Depends(require_admin)])
async def list_api_keys(db: AsyncSession = Depends(get_db)):
    keys = await api_key_crud.list_api_keys(db)
    return [
        APIKeyRead(
            id=k.id,
            masked_key=mask_api_key(k.key),
            name=k.name,
            is_active=k.is_active,
            tenant_id=k.tenant_id,
            created_at=k.created_at,
            last_used=k.last_used,
        )
        for k in keys
    ]


@router.delete("/api-keys/{api_key_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
async def delete_api_key(api_key_id: str, db: AsyncSession = Depends(get_db)):
    api_key = await api_key_crud.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API 키를 찾을 수 없습니다.")
    await api_key_crud.revoke_api_key(db, api_key)
    logger.info("api_key_revoked", api_key_id=api_key_id)


# === 일반 사용자 관리 (Sprint 6 phone-verified login) ===


@router.get("/users", response_model=list[UserRead], dependencies=[Depends(require_admin)])
async def list_users(db: AsyncSession = Depends(get_db)):
    return await user_crud.list_users(db)


@router.post("/users/{user_id}/toggle", response_model=UserRead, dependencies=[Depends(require_admin)])
async def toggle_user(user_id: str, payload: UserToggleRequest, db: AsyncSession = Depends(get_db)):
    user = await user_crud.get_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")
    user = await user_crud.set_active(db, user, payload.is_active)
    logger.info("user_toggled", user_id=user_id, is_active=payload.is_active)
    return user


@router.post(
    "/users/{user_id}/reissue-key",
    response_model=UserApiKeyReissued,
    dependencies=[Depends(require_admin)],
)
async def reissue_user_key(user_id: str, db: AsyncSession = Depends(get_db)):
    user = await user_crud.get_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")
    raw_key = generate_user_api_key()
    await user_crud.set_api_key_hash(db, user, hash_api_key(raw_key))
    logger.info("user_api_key_reissued", user_id=user_id)
    return UserApiKeyReissued(id=user.id, api_key=raw_key)
