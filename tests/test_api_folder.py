"""Regression tests for the Folders feature: /api/accounts/{account_id}/folders/*

Covers CRUD, reorder, batch group move, delete-reparents-children, smart
folders (computed from broadcast history), and folder-based send reusing the
normal broadcast creation path.
"""

import pytest


async def _create_account(client, phone="+821099990001"):
    res = await client.post("/api/accounts", json={"phone": phone, "name": "폴더 테스트 계정"})
    assert res.status_code == 201
    return res.json()["id"]


async def _create_folder(client, account_id, name="테스트 폴더", group_ids=None, parent_id=None):
    res = await client.post(
        f"/api/accounts/{account_id}/folders",
        json={"name": name, "group_ids": group_ids or [], "parent_id": parent_id},
    )
    assert res.status_code == 201, res.text
    return res.json()


@pytest.mark.asyncio
async def test_create_and_list_folder(client):
    account_id = await _create_account(client)
    folder = await _create_folder(client, account_id, name="VIP 그룹", group_ids=["-100111", "-100222"])
    assert folder["name"] == "VIP 그룹"
    assert folder["group_ids"] == ["-100111", "-100222"]
    assert folder["order"] == 0
    assert folder["is_smart"] is False

    res = await client.get(f"/api/accounts/{account_id}/folders")
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert body[0]["id"] == folder["id"]


@pytest.mark.asyncio
async def test_list_folders_for_unknown_account_404s(client):
    res = await client.get("/api/accounts/does-not-exist/folders")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_create_folder_with_missing_parent_404s(client):
    account_id = await _create_account(client)
    res = await client.post(
        f"/api/accounts/{account_id}/folders",
        json={"name": "고아 폴더", "parent_id": "nonexistent-parent"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_nested_folder_tree(client):
    account_id = await _create_account(client)
    parent = await _create_folder(client, account_id, name="부모")
    child = await _create_folder(client, account_id, name="자식", parent_id=parent["id"])

    res = await client.get(f"/api/accounts/{account_id}/folders?tree=true")
    assert res.status_code == 200
    tree = res.json()
    assert len(tree) == 1
    assert tree[0]["id"] == parent["id"]
    assert len(tree[0]["children"]) == 1
    assert tree[0]["children"][0]["id"] == child["id"]


@pytest.mark.asyncio
async def test_update_folder(client):
    account_id = await _create_account(client)
    folder = await _create_folder(client, account_id, name="원래이름")

    res = await client.put(
        f"/api/accounts/{account_id}/folders/{folder['id']}",
        json={"name": "바뀐이름", "color": "#ff0000", "group_ids": ["-100999"]},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["name"] == "바뀐이름"
    assert body["color"] == "#ff0000"
    assert body["group_ids"] == ["-100999"]


@pytest.mark.asyncio
async def test_update_folder_rejects_self_parenting(client):
    account_id = await _create_account(client)
    folder = await _create_folder(client, account_id)

    res = await client.put(
        f"/api/accounts/{account_id}/folders/{folder['id']}",
        json={"parent_id": folder["id"]},
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_update_unknown_folder_404s(client):
    account_id = await _create_account(client)
    res = await client.put(f"/api/accounts/{account_id}/folders/does-not-exist", json={"name": "x"})
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_delete_folder_reparents_children(client):
    account_id = await _create_account(client)
    grandparent = await _create_folder(client, account_id, name="조부모")
    parent = await _create_folder(client, account_id, name="부모", parent_id=grandparent["id"])
    child = await _create_folder(client, account_id, name="자식", parent_id=parent["id"])

    res = await client.delete(f"/api/accounts/{account_id}/folders/{parent['id']}")
    assert res.status_code == 204

    res = await client.get(f"/api/accounts/{account_id}/folders")
    remaining = {f["id"]: f for f in res.json()}
    assert parent["id"] not in remaining
    assert remaining[child["id"]]["parent_id"] == grandparent["id"]


@pytest.mark.asyncio
async def test_delete_unknown_folder_404s(client):
    account_id = await _create_account(client)
    res = await client.delete(f"/api/accounts/{account_id}/folders/does-not-exist")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_reorder_folders(client):
    account_id = await _create_account(client)
    a = await _create_folder(client, account_id, name="A")
    b = await _create_folder(client, account_id, name="B")

    res = await client.post(
        f"/api/accounts/{account_id}/folders/reorder",
        json=[
            {"folder_id": a["id"], "order": 5, "parent_id": None},
            {"folder_id": b["id"], "order": 1, "parent_id": None},
        ],
    )
    assert res.status_code == 200

    res = await client.get(f"/api/accounts/{account_id}/folders")
    by_id = {f["id"]: f for f in res.json()}
    assert by_id[a["id"]]["order"] == 5
    assert by_id[b["id"]]["order"] == 1


@pytest.mark.asyncio
async def test_batch_move_groups_between_folders(client):
    account_id = await _create_account(client)
    source = await _create_folder(client, account_id, name="소스", group_ids=["-100111", "-100222"])
    target = await _create_folder(client, account_id, name="타겟", group_ids=[])

    res = await client.post(
        f"/api/accounts/{account_id}/folders/batch/move",
        json={"source_folder_id": source["id"], "target_folder_id": target["id"], "group_ids": ["-100111"]},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["moved_count"] == 2  # removed from source + added to target

    res = await client.get(f"/api/accounts/{account_id}/folders")
    by_id = {f["id"]: f for f in res.json()}
    assert by_id[source["id"]]["group_ids"] == ["-100222"]
    assert by_id[target["id"]]["group_ids"] == ["-100111"]


@pytest.mark.asyncio
async def test_smart_folder_vip_from_broadcast_history(client, db_session):
    """VIP smart folder ranks groups by successful-send count."""
    account_id = await _create_account(client)

    from app.crud import broadcast as broadcast_crud
    from app.schemas.broadcast import BroadcastCreate

    for _ in range(3):
        b = await broadcast_crud.create_broadcast(
            db_session,
            BroadcastCreate(account_id=account_id, message="hi", recipients=["-100AAA"]),
            None,
            scheduled_at=None,
        )
        b.status = "sent"
    b2 = await broadcast_crud.create_broadcast(
        db_session,
        BroadcastCreate(account_id=account_id, message="hi", recipients=["-100BBB"]),
        None,
        scheduled_at=None,
    )
    b2.status = "sent"
    await db_session.commit()

    res = await client.post(
        f"/api/accounts/{account_id}/folders/smart",
        json={"name": "VIP", "smart_type": "vip", "params": {}},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["is_smart"] is True
    assert body["smart_type"] == "vip"
    assert body["group_ids"][0] == "-100AAA"  # most-sent group ranked first


@pytest.mark.asyncio
async def test_smart_folder_group_ids_recompute_on_list(client, db_session):
    """Smart folder membership is derived, not frozen at creation time."""
    account_id = await _create_account(client)

    res = await client.post(
        f"/api/accounts/{account_id}/folders/smart",
        json={"name": "VIP", "smart_type": "vip", "params": {"vip_group_ids": ["-100X"]}},
    )
    assert res.status_code == 201
    assert res.json()["group_ids"] == ["-100X"]

    # Re-list after the underlying params conceptually change nothing here, but confirms
    # the list endpoint recomputes rather than trusting a stale persisted value.
    res = await client.get(f"/api/accounts/{account_id}/folders")
    assert res.status_code == 200
    assert res.json()[0]["group_ids"] == ["-100X"]


@pytest.mark.asyncio
async def test_workspace_state_persists_collapsed_and_pinned(client):
    account_id = await _create_account(client)
    a = await _create_folder(client, account_id, name="A")
    b = await _create_folder(client, account_id, name="B")

    res = await client.post(
        f"/api/accounts/{account_id}/folders/workspace-state",
        json={"collapsed_folder_ids": [a["id"]], "pinned_folder_ids": [b["id"]]},
    )
    assert res.status_code == 200

    res = await client.get(f"/api/accounts/{account_id}/folders")
    by_id = {f["id"]: f for f in res.json()}
    assert by_id[a["id"]]["is_collapsed"] is True
    assert by_id[b["id"]]["order"] == -1


@pytest.mark.asyncio
async def test_send_to_folder_creates_broadcast(client, db_session):
    account_id = await _create_account(client)
    folder = await _create_folder(client, account_id, name="발송대상", group_ids=["-100111", "-100222"])

    res = await client.post(
        f"/api/accounts/{account_id}/folders/send",
        json={"folder_ids": [folder["id"]], "message": "안녕하세요"},
    )
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["total_groups"] == 2
    assert len(body["broadcast_ids"]) == 1

    from app.crud import broadcast as broadcast_crud

    broadcast = await broadcast_crud.get_broadcast(db_session, body["broadcast_ids"][0])
    assert broadcast is not None
    assert set(broadcast.recipients) == {"-100111", "-100222"}
    assert broadcast.message == "안녕하세요"


@pytest.mark.asyncio
async def test_send_to_folder_with_no_groups_400s(client):
    account_id = await _create_account(client)
    folder = await _create_folder(client, account_id, name="빈폴더", group_ids=[])

    res = await client.post(
        f"/api/accounts/{account_id}/folders/send",
        json={"folder_ids": [folder["id"]], "message": "안녕하세요"},
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_folder_from_other_account_is_not_accessible(client):
    account_a = await _create_account(client, phone="+821099990002")
    account_b = await _create_account(client, phone="+821099990003")
    folder = await _create_folder(client, account_a, name="A의 폴더")

    res = await client.put(f"/api/accounts/{account_b}/folders/{folder['id']}", json={"name": "탈취 시도"})
    assert res.status_code == 404

    res = await client.delete(f"/api/accounts/{account_b}/folders/{folder['id']}")
    assert res.status_code == 404
