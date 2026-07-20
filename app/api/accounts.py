from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.api.deps import get_current_identity, Identity, require_account_capacity, require_account_tenant_access, require_admin
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.database import get_db
from app.models.account import Account
from app.schemas.account import (
    AccountCreate,
    AccountFilterParams,
    AccountRead,
    AccountSortParams,
    AccountUpdate,
    AccountWithHealth,
    BulkActionRequest,
    BulkActionResult,
    BulkActionResponse,
    PaginatedAccounts,
    AccountSummary,
)
from app.services.account_health import get_account_health, get_health_summary

router = APIRouter(prefix="/api/accounts", tags=["accounts"])
logger = get_logger(__name__)


@router.get("", response_model=PaginatedAccounts)
async def read_accounts(
    search: str | None = Query(default=None, description="Search phone or name"),
    status: str | None = Query(default=None),
    health_status: str | None = Query(default=None),
    has_session: bool | None = Query(default=None),
    has_error: bool | None = Query(default=None),
    auto_reply_enabled: bool | None = Query(default=None),
    phone: str | None = Query(default=None),
    sort_by: str = Query(default="created_at"),
    sort_dir: str = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """List accounts with search, filter, sort, and pagination."""
    tenant_id = None
    if identity.kind == "admin":
        tenant_id = None
    elif identity.tenant_id:
        tenant_id = identity.tenant_id
    else:
        return PaginatedAccounts(items=[], total=0, page=page, page_size=page_size, total_pages=0)

    filters = AccountFilterParams(
        search=search,
        status=status,
        has_session=has_session,
        has_error=has_error,
        auto_reply_enabled=auto_reply_enabled,
        phone=phone,
    )
    sort = AccountSortParams(sort_by=sort_by, sort_dir=sort_dir)

    accounts, total = await account_crud.query_accounts(
        db, tenant_id=tenant_id, filters=filters, sort=sort, page=page, page_size=page_size
    )

    # Enrich with health status
    health_items = await get_account_health(identity)
    health_map = {h.account_id: h for h in health_items}

    items = []
    for a in accounts:
        health = health_map.get(a.id)
        health_status_val = health.status if health else "unknown"
        items.append(AccountWithHealth(
            id=a.id,
            phone=a.phone,
            name=a.name,
            status=a.status,
            health_status=health_status_val,
            has_session=a.session_data is not None,
            today_sent=a.today_sent,
            group_count=a.group_count,
            last_activity=a.last_activity,
            last_error=a.last_error,
            last_error_at=a.last_error_at,
            last_success_at=a.last_success_at,
            health_checked_at=a.health_checked_at,
            auto_reply_enabled=a.auto_reply_enabled,
            recent_success_count=health.recent_success_count if health else 0,
            recent_failure_count=health.recent_failure_count if health else 0,
            total_delivery_attempts=health.total_delivery_attempts if health else 0,
            created_at=a.created_at,
            updated_at=a.updated_at,
        ))

    # Apply health_status filter post-query (it's derived, not stored)
    if health_status:
        items = [i for i in items if i.health_status == health_status]

    total_pages = max(1, (total + page_size - 1) // page_size)
    return PaginatedAccounts(items=items, total=total, page=page, page_size=page_size, total_pages=total_pages)


@router.get("/summary", response_model=AccountSummary)
async def get_accounts_summary(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get operational summary of all accounts."""
    if identity.kind != "admin" and identity.tenant_id is None:
        return AccountSummary(
            total=0, healthy=0, unhealthy=0, not_configured=0, banned=0,
            rate_limited=0, unauthorized=0, active_accounts=0, inactive_accounts=0,
            has_session=0, has_errors=0, total_today_sent=0, total_groups=0,
        )

    summary = await get_health_summary(identity)
    return AccountSummary(
        total=summary.total,
        healthy=summary.healthy,
        unhealthy=summary.unhealthy,
        not_configured=summary.not_configured,
        banned=summary.banned,
        rate_limited=summary.rate_limited,
        unauthorized=summary.unauthorized,
        active_accounts=summary.total - summary.not_configured - summary.banned,
        inactive_accounts=summary.not_configured,
        has_session=summary.has_session,
        has_errors=summary.has_errors,
        total_today_sent=summary.total_today_sent,
        total_groups=summary.total_groups,
    )


@router.post("/bulk", response_model=BulkActionResponse)
async def bulk_account_action(
    payload: BulkActionRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Execute a bulk action on multiple accounts.

    Supported actions: activate, deactivate, delete, reset_session.
    """
    results = []
    total_processed = 0
    total_failed = 0

    for account_id in payload.account_ids:
        try:
            await require_account_tenant_access(account_id, db, identity)
            account = await account_crud.get_account(db, account_id)
            if account is None:
                results.append(BulkActionResult(account_id=account_id, success=False, error="계정을 찾을 수 없습니다."))
                total_failed += 1
                continue

            if payload.action == "activate":
                await account_crud.update_account(db, account, AccountUpdate(status="active"))
            elif payload.action == "deactivate":
                await account_crud.update_account(db, account, AccountUpdate(status="inactive"))
            elif payload.action == "reset_session":
                await account_crud.mark_account_session_invalid(db, account)
            elif payload.action == "delete":
                await account_crud.delete_account(db, account)
            else:
                results.append(BulkActionResult(account_id=account_id, success=False, error=f"Unknown action: {payload.action}"))
                total_failed += 1
                continue

            results.append(BulkActionResult(account_id=account_id, success=True))
            total_processed += 1
        except HTTPException as exc:
            results.append(BulkActionResult(account_id=account_id, success=False, error=exc.detail))
            total_failed += 1
        except Exception as exc:
            logger.error("bulk_action_failed", account_id=account_id, action=payload.action, error=str(exc))
            results.append(BulkActionResult(account_id=account_id, success=False, error=str(exc)))
            total_failed += 1

    return BulkActionResponse(results=results, total_processed=total_processed, total_failed=total_failed)


@router.post("", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: AccountCreate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_capacity(db, identity)
    try:
        account_data = payload.model_dump()
        if identity.tenant_id:
            account_data["tenant_id"] = identity.tenant_id
        account = await account_crud.create_account(db, AccountCreate(**account_data))
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
async def read_account(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    return account


@router.put("/{account_id}", response_model=AccountRead)
async def update_account(
    account_id: str,
    payload: AccountUpdate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    return await account_crud.update_account(db, account, payload)


@router.post("/{account_id}/clear-error", response_model=AccountRead)
async def clear_account_error(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """확인/삭제 — 마지막 오류 표시를 지운다 (새 오류가 나기 전까진 다시 안 뜸)."""
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    return await account_crud.clear_account_error(db, account)


@router.post("/{account_id}/resume", response_model=AccountRead)
async def resume_account(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    _admin_check: None = Depends(require_admin),
):
    """관리자가 suspended 계정을 active로 복구한다.

    Requires admin privileges. Clears the restriction-related error state
    so the account can send broadcasts again.
    """
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    if account.status != "suspended":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="일시중단된 계정만 재개할 수 있습니다.",
        )
    return await account_crud.resume_account(db, account)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    try:
        await account_crud.delete_account(db, account)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    logger.info("account_deleted", account_id=account_id)
