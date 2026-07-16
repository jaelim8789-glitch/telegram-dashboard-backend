from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_account_tenant_access, require_broadcast_capacity
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import broadcast as broadcast_crud
from app.crud import folder as folder_crud
from app.database import get_db
from app.models.account import Account
from app.models.folder import Folder
from app.schemas.broadcast import BroadcastCreate
from app.schemas.folder import (
    BatchMoveInput,
    FolderCreate,
    FolderRead,
    FolderReorderInput,
    FolderSendInput,
    FolderUpdate,
    SmartFolderConfig,
    WorkspaceStateInput,
)
from app.services.broadcast_processor import process_broadcast
from app.services.telegram_actions import AccountNotAuthenticatedError, get_folders, list_groups

router = APIRouter(prefix="/api/accounts/{account_id}/folders", tags=["folders"])
logger = get_logger(__name__)


async def _get_account_or_404(account_id: str, db: AsyncSession) -> Account:
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    return account


async def _get_owned_folder_or_404(account_id: str, folder_id: str, db: AsyncSession) -> Folder:
    folder = await folder_crud.get_folder(db, folder_id)
    if folder is None or folder.account_id != account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="폴더를 찾을 수 없습니다.")
    return folder


async def _compute_smart_folder_groups(db: AsyncSession, account_id: str, smart_type: str, params: dict[str, Any]) -> list[str]:
    """Best-effort — smart folders degrade to an empty group list rather than
    erroring, since they're a convenience view derived from send history."""
    broadcasts = await broadcast_crud.list_logs(db, account_id=account_id)

    if smart_type == "recent_activity":
        hours = params.get("hours", 24)
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
        active: set[str] = set()
        for b in broadcasts:
            if b.status == "sent" and b.sent_at and b.sent_at > cutoff:
                active.update(b.recipients)
        return list(active)[:50]

    if smart_type == "unsent":
        sent_ids: set[str] = set()
        failed_ids: set[str] = set()
        for b in broadcasts:
            if b.status == "sent":
                sent_ids.update(b.recipients)
            elif b.status == "failed":
                failed_ids.update(b.recipients)
        only_failed = failed_ids - sent_ids
        return list(only_failed)[:100]

    if smart_type == "vip":
        vip_ids = params.get("vip_group_ids")
        if vip_ids:
            return list(vip_ids)
        sent_counts: dict[str, int] = {}
        for b in broadcasts:
            if b.status == "sent":
                for r in b.recipients:
                    sent_counts[r] = sent_counts.get(r, 0) + 1
        ranked = sorted(sent_counts.items(), key=lambda kv: kv[1], reverse=True)
        return [gid for gid, _ in ranked[:20]]

    if smart_type == "auto_classify":
        keywords = [k.lower() for k in params.get("keywords", [])]
        if not keywords:
            return []
        exclude_ids = set(params.get("exclude_group_ids", []))
        account = await account_crud.get_account(db, account_id)
        try:
            groups = await list_groups(account) if account else []
        except AccountNotAuthenticatedError:
            groups = []
        matched = [
            g["id"] for g in groups
            if g["id"] not in exclude_ids and any(kw in g["title"].lower() for kw in keywords)
        ]
        return matched[:100]

    return []


@router.get("", response_model=list[FolderRead])
async def list_account_folders(
    account_id: str,
    tree: bool = False,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)

    folders = await folder_crud.list_folders(db, account_id)

    # Smart folders' membership is derived, not stored — recompute on every read.
    for folder in folders:
        if folder.is_smart and folder.smart_type:
            import json as _json

            params = _json.loads(folder.smart_params) if folder.smart_params else {}
            try:
                group_ids = await _compute_smart_folder_groups(db, account_id, folder.smart_type, params)
                folder.group_ids = _json.dumps(group_ids)
            except Exception as exc:
                logger.warning("smart_folder_compute_failed", account_id=account_id, folder_id=folder.id, error=str(exc))

    if tree:
        return folder_crud.build_folder_tree(folders)
    return folders


@router.post("", response_model=FolderRead, status_code=status.HTTP_201_CREATED)
async def create_account_folder(
    account_id: str,
    payload: FolderCreate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)

    if payload.parent_id is not None:
        await _get_owned_folder_or_404(account_id, payload.parent_id, db)

    folder = await folder_crud.create_folder(db, account_id, payload)
    logger.info("folder_created", account_id=account_id, folder_id=folder.id)
    return folder


@router.post("/smart", response_model=FolderRead, status_code=status.HTTP_201_CREATED)
async def create_account_smart_folder(
    account_id: str,
    payload: SmartFolderConfig,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)

    group_ids = await _compute_smart_folder_groups(db, account_id, payload.smart_type, payload.params)
    folder = await folder_crud.create_smart_folder(
        db, account_id, name=payload.name, smart_type=payload.smart_type, color=payload.color,
        icon=payload.icon, description=payload.description, params=payload.params, group_ids=group_ids,
    )
    logger.info("smart_folder_created", account_id=account_id, folder_id=folder.id, smart_type=payload.smart_type)
    return folder


@router.put("/{folder_id}", response_model=FolderRead)
async def update_account_folder(
    account_id: str,
    folder_id: str,
    payload: FolderUpdate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)
    folder = await _get_owned_folder_or_404(account_id, folder_id, db)

    if payload.parent_id is not None:
        if payload.parent_id == folder_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="폴더는 자기 자신의 하위 폴더가 될 수 없습니다.")
        await _get_owned_folder_or_404(account_id, payload.parent_id, db)

    folder = await folder_crud.update_folder(db, folder, payload)
    return folder


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account_folder(
    account_id: str,
    folder_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)
    folder = await _get_owned_folder_or_404(account_id, folder_id, db)

    await folder_crud.delete_folder(db, folder)
    logger.info("folder_deleted", account_id=account_id, folder_id=folder_id)


@router.post("/reorder", status_code=status.HTTP_200_OK)
async def reorder_account_folders(
    account_id: str,
    payload: list[FolderReorderInput],
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)

    await folder_crud.reorder_folders(db, account_id, payload)
    return {"status": "ok"}


@router.post("/batch/move", status_code=status.HTTP_200_OK)
async def batch_move_account_groups(
    account_id: str,
    payload: BatchMoveInput,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)

    moved_count = await folder_crud.batch_move_groups(db, account_id, payload)
    logger.info("folder_groups_moved", account_id=account_id, moved_count=moved_count)
    return {
        "moved_count": moved_count,
        "source_folder_id": payload.source_folder_id,
        "target_folder_id": payload.target_folder_id,
    }


@router.post("/sync", response_model=list[FolderRead])
async def sync_account_telegram_folders(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    account = await _get_account_or_404(account_id, db)

    try:
        telegram_folders = await get_folders(account)
    except AccountNotAuthenticatedError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="계정이 인증되지 않았습니다.")

    if not telegram_folders:
        return []

    folders = await folder_crud.upsert_synced_folders(db, account_id, telegram_folders)
    logger.info("folders_synced", account_id=account_id, count=len(folders))
    return folders


@router.post("/send", status_code=status.HTTP_202_ACCEPTED)
async def send_to_account_folders(
    account_id: str,
    payload: FolderSendInput,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    await require_broadcast_capacity(db, identity)
    await _get_account_or_404(account_id, db)

    import json as _json

    exclude_set = set(payload.exclude_group_ids)
    all_group_ids: list[str] = []
    for folder_id in payload.folder_ids:
        folder = await folder_crud.get_folder(db, folder_id)
        if folder is None or folder.account_id != account_id:
            continue
        if folder.is_smart and folder.smart_type:
            params = _json.loads(folder.smart_params) if folder.smart_params else {}
            group_ids = await _compute_smart_folder_groups(db, account_id, folder.smart_type, params)
        else:
            group_ids = _json.loads(folder.group_ids)
        all_group_ids.extend(gid for gid in group_ids if gid not in exclude_set)

    # De-dupe while preserving order (a group may appear in more than one selected folder).
    seen: set[str] = set()
    recipients = [gid for gid in all_group_ids if not (gid in seen or seen.add(gid))]

    if not recipients:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="선택한 폴더에 그룹이 없습니다.")

    wait_seconds = await broadcast_crud.seconds_until_next_allowed_broadcast(db, account_id)
    if wait_seconds > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"{wait_seconds}초 후 다시 시도하세요. (계정당 1분에 1회 발송 제한)",
        )

    broadcast_data = BroadcastCreate(account_id=account_id, message=payload.message, recipients=recipients)
    broadcast = await broadcast_crud.create_broadcast(db, broadcast_data, None, scheduled_at=None)
    background_tasks.add_task(process_broadcast, broadcast.id)

    logger.info("folder_broadcast_created", account_id=account_id, broadcast_id=broadcast.id, group_count=len(recipients))
    return {
        "broadcast_ids": [broadcast.id],
        "total_groups": len(recipients),
        "sent_count": len(recipients),
        "message": payload.message,
    }


@router.post("/workspace-state", status_code=status.HTTP_200_OK)
async def save_account_folder_workspace_state(
    account_id: str,
    payload: WorkspaceStateInput,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)

    await folder_crud.save_workspace_state(
        db, account_id,
        collapsed_folder_ids=payload.collapsed_folder_ids,
        pinned_folder_ids=payload.pinned_folder_ids,
    )
    return {"status": "ok"}
