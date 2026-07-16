import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.folder import Folder
from app.schemas.folder import BatchMoveInput, FolderCreate, FolderReorderInput, FolderUpdate


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _next_sort_order(db: AsyncSession, account_id: str) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(Folder.sort_order), -1)).where(Folder.account_id == account_id)
    )
    return (result.scalar_one() or -1) + 1


async def create_folder(db: AsyncSession, account_id: str, data: FolderCreate) -> Folder:
    folder = Folder(
        account_id=account_id,
        name=data.name,
        description=data.description,
        color=data.color,
        icon=data.icon,
        group_ids=json.dumps(data.group_ids),
        parent_id=data.parent_id,
        sort_order=await _next_sort_order(db, account_id),
    )
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    return folder


async def create_smart_folder(
    db: AsyncSession, account_id: str, *, name: str, smart_type: str, color: str, icon: str,
    description: str, params: dict, group_ids: list[str],
) -> Folder:
    folder = Folder(
        account_id=account_id,
        name=name,
        description=description,
        color=color,
        icon=icon,
        group_ids=json.dumps(group_ids),
        is_smart=True,
        smart_type=smart_type,
        smart_params=json.dumps(params),
        sort_order=await _next_sort_order(db, account_id),
    )
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    return folder


async def list_folders(db: AsyncSession, account_id: str) -> list[Folder]:
    result = await db.execute(
        select(Folder).where(Folder.account_id == account_id).order_by(Folder.sort_order.asc(), Folder.name.asc())
    )
    return list(result.scalars().all())


async def get_folder(db: AsyncSession, folder_id: str) -> Folder | None:
    return await db.get(Folder, folder_id)


async def update_folder(db: AsyncSession, folder: Folder, data: FolderUpdate) -> Folder:
    update_data = data.model_dump(exclude_unset=True)
    if "group_ids" in update_data and update_data["group_ids"] is not None:
        update_data["group_ids"] = json.dumps(update_data["group_ids"])
    if "order" in update_data:
        update_data["sort_order"] = update_data.pop("order")
    for field, value in update_data.items():
        setattr(folder, field, value)
    await db.commit()
    await db.refresh(folder)
    return folder


async def set_folder_group_ids(db: AsyncSession, folder: Folder, group_ids: list[str]) -> Folder:
    folder.group_ids = json.dumps(group_ids)
    await db.commit()
    await db.refresh(folder)
    return folder


async def delete_folder(db: AsyncSession, folder: Folder) -> None:
    # Reparent children to this folder's parent instead of cascading their deletion —
    # deleting a folder should never silently drop the folders nested under it.
    result = await db.execute(select(Folder).where(Folder.parent_id == folder.id))
    for child in result.scalars().all():
        child.parent_id = folder.parent_id
    await db.delete(folder)
    await db.commit()


async def reorder_folders(db: AsyncSession, account_id: str, items: list[FolderReorderInput]) -> None:
    folders_by_id = {f.id: f for f in await list_folders(db, account_id)}
    for item in items:
        folder = folders_by_id.get(item.folder_id)
        if folder is None:
            continue
        folder.sort_order = item.order
        folder.parent_id = item.parent_id
    await db.commit()


async def batch_move_groups(db: AsyncSession, account_id: str, data: BatchMoveInput) -> int:
    moved_count = 0
    move_set = set(data.group_ids)

    if data.source_folder_id:
        source = await get_folder(db, data.source_folder_id)
        if source is not None and source.account_id == account_id:
            current = set(json.loads(source.group_ids))
            updated = current - move_set
            moved_count += len(current - updated)
            source.group_ids = json.dumps(list(updated))

    if data.target_folder_id:
        target = await get_folder(db, data.target_folder_id)
        if target is not None and target.account_id == account_id:
            current = set(json.loads(target.group_ids))
            updated = current | move_set
            moved_count += len(updated - current)
            target.group_ids = json.dumps(list(updated))

    await db.commit()
    return moved_count


async def upsert_synced_folders(db: AsyncSession, account_id: str, telegram_folders: list[dict]) -> list[Folder]:
    """Sync Telegram-native chat folders (Dialog Filters) into persisted Folder rows,
    matched by name — re-running sync updates group_ids in place instead of duplicating."""
    existing = {f.name: f for f in await list_folders(db, account_id)}
    result: list[Folder] = []

    for tf in telegram_folders:
        title = tf.get("title") or "Unnamed Folder"
        group_ids = tf.get("group_ids", [])
        folder = existing.get(title)
        if folder is not None:
            folder.group_ids = json.dumps(group_ids)
        else:
            folder = Folder(
                account_id=account_id,
                name=title,
                group_ids=json.dumps(group_ids),
                sort_order=await _next_sort_order(db, account_id),
            )
            db.add(folder)
            existing[title] = folder
        result.append(folder)

    await db.commit()
    for folder in result:
        await db.refresh(folder)
    return result


async def save_workspace_state(
    db: AsyncSession, account_id: str, *, collapsed_folder_ids: list[str], pinned_folder_ids: list[str]
) -> None:
    folders_by_id = {f.id: f for f in await list_folders(db, account_id)}
    for folder_id in collapsed_folder_ids:
        folder = folders_by_id.get(folder_id)
        if folder is not None:
            folder.is_collapsed = True
    for folder_id in pinned_folder_ids:
        folder = folders_by_id.get(folder_id)
        if folder is not None:
            folder.sort_order = -1
    await db.commit()


def build_folder_tree(folders: list[Folder]) -> list[dict]:
    """Nest folders under their parent_id for the `?tree=true` response shape."""
    nodes: dict[str, dict] = {}
    for f in folders:
        nodes[f.id] = {
            "id": f.id, "account_id": f.account_id, "name": f.name, "description": f.description,
            "color": f.color, "icon": f.icon, "group_ids": json.loads(f.group_ids),
            "order": f.sort_order, "parent_id": f.parent_id, "is_collapsed": f.is_collapsed,
            "is_smart": f.is_smart, "smart_type": f.smart_type,
            "created_at": f.created_at, "updated_at": f.updated_at, "children": [],
        }

    roots: list[dict] = []
    for node in nodes.values():
        parent_id = node["parent_id"]
        if parent_id and parent_id in nodes:
            nodes[parent_id]["children"].append(node)
        else:
            roots.append(node)

    for node in nodes.values():
        node["children"].sort(key=lambda n: n["order"])
    roots.sort(key=lambda n: n["order"])
    return roots
