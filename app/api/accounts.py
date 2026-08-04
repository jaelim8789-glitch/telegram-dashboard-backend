import csv
from datetime import datetime, timezone
from io import StringIO

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
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

router = APIRouter(prefix="/api/accounts", tags=["accounts"], redirect_slashes=False)
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

    # Enrich with health status – reuse already-fetched accounts to avoid N+1
    health_items = await get_account_health(identity, preloaded_accounts=accounts)
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
            health_score=health.health_score if health else 0,
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


def _account_with_health_to_dict(account: AccountWithHealth) -> dict:
    return {
        "id": account.id,
        "phone": account.phone,
        "name": account.name,
        "status": account.status,
        "health_status": account.health_status,
        "health_score": account.health_score,
        "has_session": account.has_session,
        "today_sent": account.today_sent,
        "group_count": account.group_count,
        "last_activity": account.last_activity.isoformat() if account.last_activity else None,
        "last_error": account.last_error,
        "last_error_at": account.last_error_at.isoformat() if account.last_error_at else None,
        "last_success_at": account.last_success_at.isoformat() if account.last_success_at else None,
        "health_checked_at": account.health_checked_at.isoformat() if account.health_checked_at else None,
        "auto_reply_enabled": account.auto_reply_enabled,
        "recent_success_count": account.recent_success_count,
        "recent_failure_count": account.recent_failure_count,
        "total_delivery_attempts": account.total_delivery_attempts,
        "created_at": account.created_at.isoformat(),
        "updated_at": account.updated_at.isoformat(),
    }


@router.get("/export")
async def export_accounts(
    format: str = Query(default="json", description="json or csv"),
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
    page_size: int = Query(default=1000, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Export accounts in JSON or CSV format."""
    filters = AccountFilterParams(
        search=search,
        status=status,
        has_session=has_session,
        has_error=has_error,
        auto_reply_enabled=auto_reply_enabled,
        phone=phone,
    )
    sort = AccountSortParams(sort_by=sort_by, sort_dir=sort_dir)

    accounts, _ = await account_crud.query_accounts(
        db, tenant_id=identity.tenant_id if identity.kind != "admin" else None,
        filters=filters, sort=sort, page=page, page_size=page_size,
    )

    # Reuse already-fetched accounts to avoid N+1
    health_items = await get_account_health(identity, preloaded_accounts=accounts)
    health_map = {h.account_id: h for h in health_items}
    items = []
    for a in accounts:
        health = health_map.get(a.id)
        items.append(AccountWithHealth(
            id=a.id,
            phone=a.phone,
            name=a.name,
            status=a.status,
            health_status=health.status if health else "unknown",
            health_score=health.health_score if health else 0,
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

    rows = [ _account_with_health_to_dict(item) for item in items ]

    if format.lower() == "csv":
        buf = StringIO()
        writer = csv.writer(buf)
        header = [
            "id", "phone", "name", "status", "health_status", "health_score",
            "has_session", "today_sent", "group_count", "last_activity", "last_error",
            "last_error_at", "last_success_at", "health_checked_at", "auto_reply_enabled",
            "recent_success_count", "recent_failure_count", "total_delivery_attempts",
            "created_at", "updated_at",
        ]
        writer.writerow(header)
        for row in rows:
            writer.writerow([row[h] for h in header])
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=accounts.csv"},
        )

    return rows


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

    Supported actions: activate, deactivate, connect, disconnect, delete, reset_session, rename.
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
            elif payload.action == "rename":
                if not payload.value:
                    results.append(BulkActionResult(account_id=account_id, success=False, error="Rename value is required."))
                    total_failed += 1
                    continue
                await account_crud.update_account(db, account, AccountUpdate(name=payload.value))
            elif payload.action == "connect":
                await account_crud.update_account(db, account, AccountUpdate(status="active"))
            elif payload.action == "disconnect":
                await account_crud.update_account(db, account, AccountUpdate(status="inactive"))
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
    if identity.kind != "admin" and identity.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="테넌트 정보가 없습니다. 로그아웃 후 다시 로그인해주세요.",
        )
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

    # Pass pre-fetched account to avoid redundant query inside get_account_health
    health_items = await get_account_health(identity, preloaded_accounts=[account])
    health = health_items[0] if health_items else None

    return AccountRead(
        id=account.id,
        phone=account.phone,
        name=account.name,
        status=account.status,
        health_status=health.status if health else None,
        health_score=health.health_score if health else 0,
        has_session=account.session_data is not None,
        today_sent=account.today_sent,
        group_count=account.group_count,
        last_activity=account.last_activity,
        auto_reply_enabled=account.auto_reply_enabled,
        last_error=account.last_error,
        last_error_at=account.last_error_at,
        last_success_at=account.last_success_at,
        health_checked_at=account.health_checked_at,
        recent_success_count=health.recent_success_count if health else 0,
        recent_failure_count=health.recent_failure_count if health else 0,
        total_delivery_attempts=health.total_delivery_attempts if health else 0,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


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


@router.get("/{account_id}/runtime-metrics")
async def get_account_runtime_metrics(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get operational runtime metrics for a single account."""
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="계정을 찾을 수 없습니다.")

    # Pass pre-fetched account to avoid redundant query inside get_account_health
    health_items = await get_account_health(identity, preloaded_accounts=[account])
    health = health_items[0] if health_items else None

    now = datetime.now(timezone.utc)
    connected_time = None
    if account.last_activity:
        delta = now - account.last_activity.replace(tzinfo=timezone.utc) if account.last_activity.tzinfo is None else now - account.last_activity
        connected_time = int(delta.total_seconds())

    flood_wait = False
    if health and health.last_error_status:
        flood_wait = "flood" in (health.last_error_status or "").lower()

    return {
        "account_id": account_id,
        "status": account.status,
        "has_session": account.session_data is not None,
        "health_score": health.health_score if health else 0,
        "health_status": health.status if health else "unknown",
        "connected_seconds_ago": connected_time,
        "flood_wait": flood_wait,
        "last_error": account.last_error,
        "last_error_at": account.last_error_at.isoformat() if account.last_error_at else None,
        "last_success_at": account.last_success_at.isoformat() if account.last_success_at else None,
        "last_activity": account.last_activity.isoformat() if account.last_activity else None,
        "today_sent": account.today_sent,
        "group_count": account.group_count,
        "recent_success_count": health.recent_success_count if health else 0,
        "recent_failure_count": health.recent_failure_count if health else 0,
        "total_delivery_attempts": health.total_delivery_attempts if health else 0,
        "auto_reply_enabled": account.auto_reply_enabled,
        "created_at": account.created_at.isoformat() if account.created_at else None,
    }


@router.get("/{account_id}/events")
async def get_account_session_events(
    account_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get session event history for an account."""
    await require_account_tenant_access(account_id, db, identity)
    from app.services.session_manager import SessionManager
    manager = SessionManager()
    if not manager._initialized:
        return []
    return await manager.get_events(account_id, limit)


# ── Epic 19: Sync progress + account maintenance ───────────────────────


@router.get("/sync-progress")
async def get_sync_queue(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Sync queue: live progress for every account of the caller's tenant.

    Reads Redis sync:{account_id} keys (10-min TTL); accounts without an
    active sync still appear with their terminal facts from the DB so the
    queue UI can show Completed / Waiting states.
    """
    from app.services import sync_progress

    result = await db.execute(select(Account).where(Account.tenant_id == identity.tenant_id))
    accounts = result.scalars().all()

    queue = []
    for acc in accounts:
        live = await sync_progress.get_progress(acc.id)
        queue.append({
            "account_id": acc.id,
            "phone": acc.phone,
            "name": acc.name,
            "status": acc.status,
            "progress": live,
            "dialogs": acc.dialog_count or 0,
            "messages": acc.message_count or 0,
            "last_sync_at": acc.last_sync_at.isoformat() if acc.last_sync_at else None,
            "group_count": acc.group_count or 0,
        })
    return {"accounts": queue}


@router.get("/{account_id}/sync-progress")
async def get_account_sync_progress(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Live sync progress for a single account (or null when idle)."""
    await require_account_tenant_access(account_id, db, identity)
    from app.services import sync_progress

    return await sync_progress.get_progress(account_id)


async def _maintenance(account_id: str, action: str, db: AsyncSession, identity: Identity) -> dict:
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    from app.services.session_manager import SessionManager

    manager = SessionManager()
    session = ""
    if account.session_data:
        from app.core.crypto import decrypt_session
        try:
            session = decrypt_session(account.session_data)
        except ValueError:
            session = ""

    if action == "reconnect":
        await manager.reconnect(account_id, session)
        logger.info("account_maintenance", action="reconnect", account_id=account_id)
        return {"ok": True, "action": action}
    if action == "restart-session":
        await manager.disconnect(account_id)
        await manager.connect(account_id, session)
        logger.info("account_maintenance", action="restart_session", account_id=account_id)
        return {"ok": True, "action": action}
    if action == "restart-worker":
        # Disconnect + reconnect the account's connection worker.
        await manager.reconnect(account_id, session)
        logger.info("account_maintenance", action="restart_worker", account_id=account_id)
        return {"ok": True, "action": action}
    if action == "refresh-health":
        from app.services.account_health import get_account_health
        items = await get_account_health(identity, preloaded_accounts=[account])
        return {"ok": True, "action": action, "health": items[0].__dict__ if items and hasattr(items[0], "__dict__") else None}
    if action == "rebuild-dialogs":
        try:
            from app.services.telegram_actions import list_groups
            groups = await list_groups(account)
            account.group_count = len(groups)
            await db.commit()
            return {"ok": True, "action": action, "groups": len(groups)}
        except Exception as e:
            logger.warning("account_maintenance_rebuild_failed", account_id=account_id, error=str(e))
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"대화 목록 재구축 실패: {e}")
    if action == "clear-cache":
        try:
            await manager.disconnect(account_id)
            await manager.connect(account_id, session)
        except Exception:
            pass
        logger.info("account_maintenance", action="clear_cache", account_id=account_id)
        return {"ok": True, "action": action}

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="지원하지 않는 동작입니다.")


@router.post("/{account_id}/maintenance")
async def run_account_maintenance(
    account_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Account Maintenance Center action.

    body: {"action": "reconnect" | "restart-session" | "restart-worker" |
                          "refresh-health" | "rebuild-dialogs" | "clear-cache"}
    """
    action = str(body.get("action", "")).strip()
    if not action:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="action이 필요합니다.")
    return await _maintenance(account_id, action, db, identity)
