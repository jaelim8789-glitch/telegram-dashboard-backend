from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, require_admin
from app.config import settings
from app.core.logging import get_logger
from app.core.rate_limiter import check_rate_limit, get_client_ip, get_retry_after_seconds
from app.core.security import create_access_token, generate_user_api_key, hash_api_key, mask_api_key, verify_admin_credentials
from app.core.time import utcnow_naive
from app.crud import api_key as api_key_crud
from app.crud import user as user_crud
from app.database import get_db
from app.models.audit_log import AdminAuditLog
from app.models.tenant import Tenant
from app.models.system_setting import SystemSetting
from app.schemas.admin import (
    AdminAuditLogListResponse,
    AdminAuditLogRead,
    AdminUserBillingUpdateRequest,
    AdminUserBillingUpdateResponse,
    AdminDashboardStatusResponse,
    AdminLoginRequest,
    AdminMeResponse,
    AdminTokenResponse,
    GuideHubPublishResponse,
    ManualIssueRequest,
    ManualIssueResponse,
    UserLookupResponse,
)
from app.schemas.style_profile import StyleProfileCreate, StyleProfileAnalyzeRequest, StyleProfileUpdate, StyleProfileRead
from app.services.ai_style_service import analyze_style, list_profiles, get_profile, update_profile, delete_profile
from app.services.telegram_actions import AccountNotAuthenticatedError
from app.services.guide_hub_service import GuideHubUnavailable, publish_or_update_guide_hub
from app.services.usage_tracker import apply_plan_limits
from app.services.account_health import get_health_summary
from app.schemas.api_key import APIKeyCreated, APIKeyCreateRequest, APIKeyRead
from app.schemas.user import UserApiKeyReissued, UserRead, UserToggleRequest
from app.models.message_log import MessageLog
from app.models.referral import ReferralCommission
from app.models.token import TokenBalance, TokenTransaction
from datetime import timedelta

router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = get_logger(__name__)


async def append_admin_audit(
    db: AsyncSession,
    *,
    action: str,
    target_type: str,
    target_id: str | None,
    target_phone: str | None,
    detail: str,
    memo: str | None = None,
    result: str = "success",
    admin_username: str | None = None,
) -> None:
    db.add(
        AdminAuditLog(
            admin_username=admin_username or settings.admin_username,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_phone=target_phone,
            detail=detail,
            memo=memo,
            result=result,
        )
    )


@router.post("/login", response_model=AdminTokenResponse)
async def login(payload: AdminLoginRequest, request: Request):
    client_ip = get_client_ip(request)
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
    # Resolve user from tenant_id before key creation so we can check for conflicts
    user = None
    if payload.tenant_id:
        result = await db.execute(select(Tenant).where(Tenant.id == payload.tenant_id))
        tenant = result.scalar_one_or_none()
        if tenant is not None:
            user = await user_crud.get_user_by_phone(db, tenant.phone)
            if user is not None and user.api_key_hash is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="이 사용자에게 이미 발급된 API 키가 있습니다. 먼저 기존 키를 해지하거나 재발급해주세요.",
                )

    api_key = await api_key_crud.create_api_key(db, payload.name, tenant_id=payload.tenant_id, purpose="admin_managed")

    # Bridge: store the same key's hash in User.api_key_hash so that
    # /auth/login-with-api-key (which only checks User.api_key_hash) can
    # authenticate this key.  Without this, admin-issued API keys are
    # usable via X-API-Key header but unusable for login-with-api-key.
    if user is not None:
        user.api_key_hash = hash_api_key(api_key.key)
        await db.flush()

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
    # Also clear the User.api_key_hash bridge so the key can't login-with-api-key
    if api_key.tenant_id:
        result = await db.execute(select(Tenant).where(Tenant.id == api_key.tenant_id))
        tenant = result.scalar_one_or_none()
        if tenant is not None:
            user = await user_crud.get_user_by_phone(db, tenant.phone)
            if user is not None and user.api_key_hash is not None:
                user.api_key_hash = None
                await db.flush()
    await api_key_crud.revoke_api_key(db, api_key)
    logger.info("api_key_revoked", api_key_id=api_key_id)


@router.get("/users", response_model=list[UserRead], dependencies=[Depends(require_admin)])
async def list_users(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """List all users with plan/subscription/account-count info."""
    users_with_info = await user_crud.list_users_with_tenant_info(db, skip=skip, limit=limit)
    return [
        UserRead(
            id=u.user.id,
            phone=u.user.phone,
            is_active=u.user.is_active,
            created_at=u.user.created_at,
            last_login=u.user.last_login,
            plan=u.plan,
            subscription_status=u.subscription_status,
            trial_expires_at=u.trial_expires_at,
            account_count=u.account_count,
            stars_balance=u.stars_balance,
        )
        for u in users_with_info
    ]


@router.post("/users/{user_id}/toggle", response_model=UserRead, dependencies=[Depends(require_admin)])
async def toggle_user(user_id: str, payload: UserToggleRequest, db: AsyncSession = Depends(get_db)):
    user = await user_crud.get_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")
    user = await user_crud.set_active(db, user, payload.is_active)
    await append_admin_audit(
        db,
        action="user_toggle",
        target_type="user",
        target_id=user.id,
        target_phone=user.phone,
        detail=f"User active toggled to {payload.is_active}",
    )
    await db.commit()
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


@router.get("/user-lookup", response_model=UserLookupResponse | None, dependencies=[Depends(require_admin)])
async def user_lookup(q: str = Query(min_length=1, max_length=50), db: AsyncSession = Depends(get_db)):
    """Look up a user by phone or tg_<telegram_user_id> identifier.
    Returns the user's current state including verification and tenant info."""
    user = await user_crud.get_user_by_phone(db, q)
    if user is None:
        return None

    result = UserLookupResponse(
        user_id=user.id,
        phone=user.phone,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login=user.last_login,
        has_api_key=user.api_key_hash is not None,
    )

    # Try telegram_user_id derived from tg_ prefix
    tg_user_id: int | None = None
    if q.startswith("tg_"):
        try:
            tg_user_id = int(q[3:])
        except ValueError:
            pass
    elif q.startswith("+") or q.isdigit():
        try:
            tg_user_id = int(q)
        except ValueError:
            pass

    if tg_user_id is not None:
        tresult = await db.execute(
            select(TelegramChannelVerification)
            .where(TelegramChannelVerification.telegram_user_id == tg_user_id)
            .order_by(TelegramChannelVerification.created_at.desc())
            .limit(1)
        )
        tcv = tresult.scalar_one_or_none()
        if tcv is not None:
            result.telegram_verification_status = tcv.status
            result.telegram_user_id = tcv.telegram_user_id
            result.telegram_verified_at = tcv.verified_at
    else:
        # Also try to find a telegram_user_id from phone pattern (tg_<id>)
        if user.phone.startswith("tg_"):
            try:
                tid = int(user.phone[3:])
                tresult = await db.execute(
                    select(TelegramChannelVerification)
                    .where(TelegramChannelVerification.telegram_user_id == tid)
                    .order_by(TelegramChannelVerification.created_at.desc())
                    .limit(1)
                )
                tcv = tresult.scalar_one_or_none()
                if tcv is not None:
                    result.telegram_verification_status = tcv.status
                    result.telegram_user_id = tcv.telegram_user_id
                    result.telegram_verified_at = tcv.verified_at
            except ValueError:
                pass

    # Look up tenant by phone
    tresult = await db.execute(select(Tenant).where(Tenant.phone == user.phone).limit(1))
    tenant = tresult.scalar_one_or_none()
    if tenant is not None:
        result.tenant_id = tenant.id
        result.tenant_plan = tenant.plan
        result.trial_expires_at = tenant.trial_expires_at
        result.subscription_status = tenant.subscription_status

    return result


@router.post("/manual-issue-key", response_model=ManualIssueResponse, dependencies=[Depends(require_admin)])
async def manual_issue_key(
    payload: ManualIssueRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    identifier = payload.user_identifier.strip()
    admin_username = settings.admin_username

    user = await user_crud.get_user_by_phone(db, identifier)
    if user is None and identifier.startswith("tg_"):
        user = await user_crud.get_user_by_phone(db, identifier)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 사용자를 찾을 수 없습니다. 먼저 회원가입을 진행해주세요.",
        )

    if user.api_key_hash is not None:
        logger.info("manual_issue_duplicate_prevented", user_id=user.id, identifier=identifier)
        return ManualIssueResponse(
            user_id=user.id,
            phone=user.phone,
            api_key="",
            already_issued=True,
        )

    # 3. Ensure tenant exists
    tresult = await db.execute(select(Tenant).where(Tenant.phone == user.phone).limit(1))
    tenant = tresult.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 사용자의 테넌트(구독)를 찾을 수 없습니다. 먼저 회원가입을 완료해주세요.",
        )

    # 3b. Admin-issued keys default to "team" (effectively unlimited) rather than
    # silently inheriting whatever plan the tenant signed up under — an admin
    # can still pick a specific plan via payload.plan when that's the intent.
    try:
        await apply_plan_limits(db, tenant, payload.plan or "team")
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="알 수 없는 플랜입니다.")

    # 4. Issue the API key
    raw_key = generate_user_api_key()
    user.api_key_hash = hash_api_key(raw_key)
    await db.flush()

    await db.commit()
    await db.refresh(user)

    # 5. Audit log (never store the raw key)
    await db.execute(
        AdminAuditLog.__table__.insert().values(
            admin_username=admin_username,
            action="manual_api_key_issue",
            target_type="user",
            target_id=user.id,
            target_phone=user.phone,
            detail=f"Issued new API key for user {user.phone} via manual admin action",
            memo=payload.memo,
            result="success",
        )
    )
    await db.commit()

    logger.info("manual_api_key_issued", user_id=user.id, identifier=identifier, memo=payload.memo)
    return ManualIssueResponse(user_id=user.id, phone=user.phone, api_key=raw_key)


@router.post(
    "/guide-hub/publish",
    response_model=GuideHubPublishResponse,
    dependencies=[Depends(require_admin)],
)
async def publish_guide_hub(db: AsyncSession = Depends(get_db)):
    """(Re-)publish the pinned 이용 가이드 허브 message in the official channel.

    First call posts and pins a new message; every later call edits that same
    message in place. Admin-only — this posts to the public official channel.
    """
    try:
        chat_id, message_id, created = await publish_or_update_guide_hub(db)
    except GuideHubUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    logger.info("guide_hub_publish_requested", chat_id=chat_id, message_id=message_id, created=created)
    return GuideHubPublishResponse(chat_id=chat_id, message_id=message_id, created=created)


@router.post(
    "/style-profiles/analyze",
    response_model=StyleProfileRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_style_profile(payload: StyleProfileAnalyzeRequest, db: AsyncSession = Depends(get_db)):
    """분석할 텍스트를 받아 AI 말투 분석을 수행하고 스타일 프로필을 저장합니다.
    
    source_type=text: source_text 필드에 직접 텍스트를 붙여넣습니다.
    source_type=channel: account_id + chat_id로 채널을 지정하면 최근 메시지를 자동 수집합니다.
    """
    try:
        profile = await analyze_style(
            name=payload.name,
            source_type=payload.source_type,
            source_text=payload.source_text,
            db=db,
            account_id=payload.account_id,
            chat_id=payload.chat_id,
            message_limit=payload.message_limit,
        )
        await db.commit()
        logger.info("style_profile_created", profile_id=profile.id, name=payload.name)
        return profile
    except AccountNotAuthenticatedError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="해당 텔레그램 계정이 인증되지 않았습니다. 계정 설정에서 다시 로그인해주세요.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get(
    "/style-profiles",
    response_model=list[StyleProfileRead],
    dependencies=[Depends(require_admin)],
)
async def list_style_profiles(db: AsyncSession = Depends(get_db)):
    """저장된 스타일 프로필 목록을 조회합니다."""
    return await list_profiles(db)


@router.get(
    "/style-profiles/{profile_id}",
    response_model=StyleProfileRead,
    dependencies=[Depends(require_admin)],
)
async def get_style_profile(profile_id: str, db: AsyncSession = Depends(get_db)):
    """특정 스타일 프로필을 조회합니다."""
    profile = await get_profile(db, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="스타일 프로필을 찾을 수 없습니다.")
    return profile


@router.patch(
    "/style-profiles/{profile_id}",
    response_model=StyleProfileRead,
    dependencies=[Depends(require_admin)],
)
async def update_style_profile(profile_id: str, payload: StyleProfileUpdate, db: AsyncSession = Depends(get_db)):
    """스타일 프로필 이름을 수정합니다."""
    profile = await get_profile(db, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="스타일 프로필을 찾을 수 없습니다.")
    if payload.name is not None:
        profile = await update_profile(db, profile, payload.name)
        await db.commit()
    return profile


@router.delete(
    "/style-profiles/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def delete_style_profile(profile_id: str, db: AsyncSession = Depends(get_db)):
    """스타일 프로필을 삭제합니다."""
    profile = await get_profile(db, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="스타일 프로필을 찾을 수 없습니다.")
    await delete_profile(db, profile)
    await db.commit()


# ── 관리자 토큰 충전 ────────────────────────────────────────────────

@router.post(
    "/users/{user_id}/topup-tokens",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_admin)],
)
async def admin_topup_tokens(
    user_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """관리자가 특정 사용자의 토큰을 수동으로 충전합니다.

    Request body: {"amount": 500, "memo": "버그 보상"}
    """
    amount = body.get("amount", 0)
    memo = body.get("memo", "관리자 수동 충전")

    if amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효하지 않은 토큰 수량입니다.")

    user = await user_crud.get_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")

    # 토큰 잔액 조회/생성
    result = await db.execute(select(TokenBalance).where(TokenBalance.user_id == user_id))
    balance = result.scalar_one_or_none()
    if balance is None:
        balance = TokenBalance(user_id=user_id, balance=0, lifetime_earned=0)
        db.add(balance)
        await db.flush()

    balance.balance += amount
    balance.lifetime_earned += amount

    # 트랜잭션 기록
    tx = TokenTransaction(
        user_id=user_id,
        amount=amount,
        balance_after=balance.balance,
        reason="admin_topup",
        memo=memo[:500] if memo else None,
    )
    db.add(tx)
    await db.commit()

    await append_admin_audit(
        db,
        action="token_topup",
        target_type="user",
        target_id=user.id,
        target_phone=user.phone,
        detail=f"Admin token top-up amount={amount}, balance_after={balance.balance}",
        memo=memo,
    )
    await db.commit()

    logger.info("admin_token_topup", user_id=user_id, amount=amount, memo=memo)
    return {"user_id": user_id, "amount": amount, "new_balance": balance.balance}


@router.patch(
    "/users/{user_id}/billing",
    response_model=AdminUserBillingUpdateResponse,
    dependencies=[Depends(require_admin)],
)
async def admin_update_user_billing(
    user_id: str,
    payload: AdminUserBillingUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Admin billing operation for a user: plan/subscription/trial adjustments."""
    if payload.trial_expires_at is not None and payload.extend_trial_days is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="trial_expires_at와 extend_trial_days는 동시에 지정할 수 없습니다.",
        )

    user = await user_crud.get_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")

    tresult = await db.execute(select(Tenant).where(Tenant.phone == user.phone).limit(1))
    tenant = tresult.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자 테넌트를 찾을 수 없습니다.")

    if payload.plan is not None:
        try:
            await apply_plan_limits(db, tenant, payload.plan)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="알 수 없는 플랜입니다.")

    if payload.subscription_status is not None:
        tenant.subscription_status = payload.subscription_status

    if payload.trial_expires_at is not None:
        tenant.trial_expires_at = payload.trial_expires_at
    elif payload.extend_trial_days is not None:
        base = tenant.trial_expires_at or utcnow_naive()
        tenant.trial_expires_at = base + timedelta(days=payload.extend_trial_days)

    await db.commit()
    await db.refresh(tenant)

    await append_admin_audit(
        db,
        action="user_billing_update",
        target_type="tenant",
        target_id=tenant.id,
        target_phone=user.phone,
        detail=(
            f"Billing updated plan={tenant.plan}, subscription_status={tenant.subscription_status}, "
            f"trial_expires_at={tenant.trial_expires_at.isoformat() if tenant.trial_expires_at else None}"
        ),
    )
    await db.commit()

    return AdminUserBillingUpdateResponse(
        user_id=user.id,
        tenant_id=tenant.id,
        phone=user.phone,
        plan=tenant.plan,
        subscription_status=tenant.subscription_status,
        trial_expires_at=tenant.trial_expires_at,
    )


# ── 사용자 삭제 (전화번호 기반) ──────────────────────────────────────

@router.delete(
    "/users/by-phone/{phone}",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_admin)],
)
async def delete_user_by_phone(
    phone: str,
    db: AsyncSession = Depends(get_db),
):
    """전화번호로 사용자를 완전히 삭제합니다. 관련 세션, 테넌트, Telegram 계정, API 키도 함께 정리합니다."""
    user = await user_crud.get_user_by_phone(db, phone)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="해당 전화번호의 사용자를 찾을 수 없습니다.")

    # 세션 삭제
    await db.execute(
        sa_text("DELETE FROM user_sessions WHERE user_id = :user_id"),
        {"user_id": user.id},
    )

    # 등록된 Telegram 계정(userbot) 삭제 — accounts.phone에 UNIQUE 제약이 있어서
    # 이걸 지우지 않으면 같은 번호로 재등록 시 "이미 등록된 전화번호입니다" 409가 남는다.
    await db.execute(
        sa_text("DELETE FROM accounts WHERE phone = :phone"),
        {"phone": phone},
    )

    # Tenant 삭제
    await db.execute(
        sa_text("DELETE FROM tenants WHERE phone = :phone"),
        {"phone": phone},
    )

    # 사용자 삭제
    await append_admin_audit(
        db,
        action="user_delete_by_phone",
        target_type="user",
        target_id=user.id,
        target_phone=phone,
        detail="Admin hard-deleted user, tenant, sessions and linked account rows by phone",
    )
    await db.delete(user)
    await db.commit()

    logger.info("admin_user_deleted_by_phone", user_id=user.id, phone=phone)
    return {"deleted": True, "user_id": user.id, "phone": phone}


# ── 대시보드 ────────────────────────────────────────────────────────

@router.get("/dashboard/status", response_model=AdminDashboardStatusResponse, dependencies=[Depends(require_admin)])
async def get_admin_dashboard_status(db: AsyncSession = Depends(get_db), identity = Depends(get_current_identity)):
    """Get aggregated real-time status for the admin dashboard."""
    users = await user_crud.list_users(db)
    active_users = [u for u in users if u.is_active]
    inactive_users = [u for u in users if not u.is_active]

    summary = await get_health_summary(identity)

    since = utcnow_naive() - timedelta(hours=24)
    result = await db.execute(
        select(
            func.count(MessageLog.id).label("total"),
            func.sum(case((MessageLog.success.is_(False), 1), else_=0)).label("failed"),
        ).where(MessageLog.created_at >= since)
    )


@router.get("/audit-logs", response_model=AdminAuditLogListResponse, dependencies=[Depends(require_admin)])
async def list_admin_audit_logs(
    limit: int = Query(default=50, ge=1, le=200),
    action: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc()).limit(limit)
    if action:
        query = query.where(AdminAuditLog.action == action)
    result = await db.execute(query)
    rows = result.scalars().all()
    return AdminAuditLogListResponse(
        items=[
            AdminAuditLogRead(
                id=row.id,
                admin_username=row.admin_username,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                target_phone=row.target_phone,
                detail=row.detail,
                memo=row.memo,
                result=row.result,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )
    row = result.one_or_none()
    recent_total = row.total or 0 if row else 0
    recent_failed = row.failed or 0 if row else 0
    failure_rate = round((recent_failed / recent_total * 100), 1) if recent_total > 0 else 0.0

    return AdminDashboardStatusResponse(
        users=AdminDashboardUserStats(
            total=len(users),
            active=len(active_users),
            inactive=len(inactive_users),
        ),
        accounts=AdminDashboardAccountStats(
            total=summary.total,
            healthy=summary.healthy,
            unhealthy=summary.unhealthy,
            not_configured=summary.not_configured,
            banned=summary.banned,
            rate_limited=summary.rate_limited,
            unauthorized=summary.unauthorized,
            error_count=summary.error_count,
            unknown=summary.unknown,
            has_session=summary.has_session,
            has_errors=summary.has_errors,
            total_today_sent=summary.total_today_sent,
            total_groups=summary.total_groups,
        ),
        broadcasts=AdminDashboardBroadcastStats(
            recent_total=recent_total,
            recent_failed=recent_failed,
            failure_rate=failure_rate,
        ),
    )


@router.get("/referral/commissions")
async def list_referral_commissions(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_admin),
):
    query = select(ReferralCommission).order_by(ReferralCommission.created_at.desc())
    if status:
        query = query.where(ReferralCommission.status == status)
    result = await db.execute(query)
    commissions = result.scalars().all()
    return {
        "items": [
            {
                "id": c.id,
                "referrer_id": c.referrer_id,
                "referred_id": c.referred_id,
                "payment_id": c.payment_id,
                "amount_cents": c.amount_cents,
                "rate": c.rate,
                "status": c.status,
                "payment_tx_id": c.payment_tx_id,
                "paid_at": c.paid_at.isoformat() if c.paid_at else None,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in commissions
        ]
    }


@router.post("/referral/commissions/{commission_id}/approve")
async def approve_referral_commission(
    commission_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_admin),
):
    commission = await db.get(ReferralCommission, commission_id)
    if commission is None:
        raise HTTPException(status_code=404, detail="커미션을 찾을 수 없습니다.")
    if commission.status != "pending":
        raise HTTPException(status_code=400, detail="이미 처리된 커미션입니다.")

    commission.status = "paid"
    commission.payment_tx_id = body.get("payment_tx_id")
    commission.paid_at = utcnow_naive()
    await db.commit()
    await db.refresh(commission)

    await append_admin_audit(
        db,
        action="referral_commission_approve",
        target_type="referral_commission",
        target_id=commission.id,
        target_phone=None,
        detail=f"Commission approved with payment_tx_id={commission.payment_tx_id}",
    )
    await db.commit()

    logger.info("referral_commission_approved", commission_id=commission.id)
    return {"ok": True, "commission_id": commission.id, "status": "paid"}



# ── System Settings ─────────────────────────────────────────────────

@router.get("/settings/watermark", dependencies=[Depends(require_admin)])
async def get_watermark_setting(
    db: AsyncSession = Depends(get_db),
):
    """워터마크 광고 문구 조회"""
    stmt = select(SystemSetting).where(SystemSetting.key == "watermark_ad")
    result = await db.execute(stmt)
    setting = result.scalar_one_or_none()
    if setting:
        return {"key": "watermark_ad", "value": setting.value, "description": setting.description}
    # Return default watermark text
    default = (
        "\n\n━━━━━━━━━━━━━━━━━━\n"
        "🤖 TeleMon AI\n\n"
        "🚀 Telegram 운영, 아직도 직접 하시나요?\n\n"
        "AI 비서가\n"
        "✅ 자동 홍보\n"
        "✅ 자동 답장\n"
        "✅ 채널 운영\n"
        "✅ 그룹 관리\n\n"
        "🌐 https://telemon.online"
    )
    return {"key": "watermark_ad", "value": default, "description": "무료 요금제 발송 시 하단에 자동 추가되는 광고 문구. 빈 문자열로 설정하면 비활성화됩니다."}


@router.put("/settings/watermark", dependencies=[Depends(require_admin)])
async def update_watermark_setting(
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """워터마크 광고 문구 수정 (빈 문자열 = 비활성화)"""
    value = body.get("value", "")
    stmt = select(SystemSetting).where(SystemSetting.key == "watermark_ad")
    result = await db.execute(stmt)
    setting = result.scalar_one_or_none()

    if setting:
        setting.value = value
    else:
        setting = SystemSetting(
            key="watermark_ad",
            value=value,
            description="무료 요금제 발송 시 하단에 자동 추가되는 광고 문구",
        )
        db.add(setting)

    await db.commit()
    logger.info("watermark_ad_updated", length=len(value))
    return {"ok": True, "key": "watermark_ad", "value": value}
