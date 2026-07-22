"""Runtime Inspector API — account session health inspection and recovery."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.database import get_db
from app.services.telegram_actions import AccountNotAuthenticatedError, get_authorized_client

router = APIRouter(prefix="/api/runtime", tags=["runtime"])
logger = get_logger(__name__)


@router.get("/inspector")
async def list_inspector_summary(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """Return a list of all accounts with their health status."""
    accounts, _ = await account_crud.query_accounts(db, tenant_id=identity.tenant_id, page=1, page_size=9999)

    total = len(accounts)
    active = sum(1 for a in accounts if a.status == "active")
    healthy = sum(1 for a in accounts if a.session_data and a.status == "active")
    unauthorized = sum(1 for a in accounts if a.session_data and a.status != "active")
    rate_limited = 0
    banned = sum(1 for a in accounts if a.status == "banned")
    error = sum(1 for a in accounts if a.last_error is not None)

    runtimes = []
    for acc in accounts:
        runtimes.append({
            "account_id": acc.id,
            "phone": acc.phone[-4:] if acc.phone else "N/A",
            "name": acc.name or None,
            "status": "healthy" if (acc.session_data and acc.status == "active") else "error" if acc.last_error else acc.status,
            "running": bool(acc.session_data and acc.status == "active"),
            "health_status": "healthy" if (acc.session_data and acc.status == "active") else "error" if acc.last_error else "inactive",
            "has_session": bool(acc.session_data),
            "uptime_seconds": 0,
            "today_sent": acc.today_sent if hasattr(acc, "today_sent") and acc.today_sent else 0,
            "group_count": acc.group_count if hasattr(acc, "group_count") and acc.group_count else 0,
            "active_broadcasts": 0,
            "queue_size": 0,
            "consecutive_failures": 0,
            "recovery_attempts": 0,
            "last_recovery_result": "",
        })

    return {
        "total": total,
        "active": active,
        "healthy": healthy,
        "unauthorized": unauthorized,
        "rate_limited": rate_limited,
        "banned": banned,
        "error": error,
        "runtimes": runtimes,
    }


@router.get("/inspector/{account_id}")
async def get_inspector_detail(
    account_id: str,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """Return detailed account info including session health."""
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    require_account_tenant_access(identity, account)

    session_valid = False
    session_error = None
    if account.session_data and account.status == "active":
        try:
            client = await get_authorized_client(account)
            me = await client.get_me()
            session_valid = me is not None
        except (AccountNotAuthenticatedError, Exception) as e:
            session_valid = False
            session_error = str(e)

    return {
        "account_id": account.id,
        "phone": account.phone,
        "name": account.name or None,
        "status": "healthy" if session_valid else "error" if account.session_data else "not_configured",
        "running": bool(account.session_data and account.status == "active"),
        "started_at": None,
        "uptime_seconds": 0,
        "health": {"session_valid": session_valid, "session_error": session_error, "last_error": account.last_error, "last_error_at": account.last_error_at.isoformat() if account.last_error_at else None, "health_checked_at": account.health_checked_at.isoformat() if account.health_checked_at else None},
        "rate_limiter": {},
        "group_cache": {},
        "broadcast_queue": {},
        "auto_reply": {"enabled": account.auto_reply_enabled if hasattr(account, "auto_reply_enabled") else False},
        "reply_macros": {},
        "session": {"has_session": bool(account.session_data), "is_active": account.status == "active"},
        "today_sent": account.today_sent if hasattr(account, "today_sent") and account.today_sent else 0,
    }


@router.post("/inspector/{account_id}/recover")
async def recover_session(
    account_id: str,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """Attempt to recover a session by reconnecting."""
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    require_account_tenant_access(identity, account)

    try:
        client = await get_authorized_client(account)
        me = await client.get_me()
        if me:
            return {"account_id": account_id, "recovered": True, "health_status": "healthy"}
        else:
            return {"account_id": account_id, "recovered": False, "health_status": "error"}
    except AccountNotAuthenticatedError:
        return {"account_id": account_id, "recovered": False, "health_status": "unauthorized"}
    except Exception as e:
        logger.error("session_recover_error", account_id=account_id, error=str(e))
        return {"account_id": account_id, "recovered": False, "health_status": "error"}


@router.post("/inspector/{account_id}/restart")
async def restart_runtime(
    account_id: str,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """Restart the account connection (disconnect + reconnect)."""
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    require_account_tenant_access(identity, account)

    try:
        from app.services.telethon_pool import pool
        await pool.disconnect(account_id)
        client = await get_authorized_client(account)
        me = await client.get_me()
        if me:
            return {"account_id": account_id, "restarted": True, "authenticated": True}
        return {"account_id": account_id, "restarted": True, "authenticated": False}
    except Exception as e:
        logger.error("runtime_restart_error", account_id=account_id, error=str(e))
        return {"account_id": account_id, "restarted": False, "authenticated": False}
