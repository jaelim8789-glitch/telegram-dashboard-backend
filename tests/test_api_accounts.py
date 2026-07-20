"""Sprint 21+ tests for Account Operations Center (search, filter, sort, paginate, bulk, summary)."""

import pytest

from app.main import app


@pytest.mark.asyncio
async def test_list_accounts_empty(client):
    res = await client.get("/api/accounts")
    assert res.status_code == 200
    body = res.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["page"] == 1


@pytest.mark.asyncio
async def test_create_account(client):
    res = await client.post("/api/accounts", json={"phone": "+821011112222", "name": "테스트 계정"})
    assert res.status_code == 201
    body = res.json()
    assert body["phone"] == "+821011112222"
    assert body["name"] == "테스트 계정"
    assert body["status"] == "inactive"
    assert body["today_sent"] == 0
    assert "session_data" not in body


@pytest.mark.asyncio
async def test_create_account_without_name(client):
    res = await client.post("/api/accounts", json={"phone": "+821022223333"})
    assert res.status_code == 201
    assert res.json()["name"] is None


@pytest.mark.asyncio
async def test_create_account_duplicate_phone_conflicts(client):
    payload = {"phone": "+821033334444", "name": "A"}
    first = await client.post("/api/accounts", json=payload)
    assert first.status_code == 201
    second = await client.post("/api/accounts", json={"phone": "+821033334444", "name": "B"})
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_get_account(client):
    created = await client.post("/api/accounts", json={"phone": "+821044445555"})
    account_id = created.json()["id"]
    res = await client.get(f"/api/accounts/{account_id}")
    assert res.status_code == 200
    assert res.json()["id"] == account_id


@pytest.mark.asyncio
async def test_get_account_not_found(client):
    res = await client.get("/api/accounts/does-not-exist")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_update_account(client):
    created = await client.post("/api/accounts", json={"phone": "+821055556666"})
    account_id = created.json()["id"]
    res = await client.put(f"/api/accounts/{account_id}", json={"name": "새 이름", "status": "active"})
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "새 이름"
    assert body["status"] == "active"


@pytest.mark.asyncio
async def test_update_account_not_found(client):
    res = await client.put("/api/accounts/does-not-exist", json={"name": "x"})
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_delete_account(client):
    created = await client.post("/api/accounts", json={"phone": "+821066667777"})
    account_id = created.json()["id"]
    res = await client.delete(f"/api/accounts/{account_id}")
    assert res.status_code == 204
    follow_up = await client.get(f"/api/accounts/{account_id}")
    assert follow_up.status_code == 404


@pytest.mark.asyncio
async def test_delete_account_not_found(client):
    res = await client.delete("/api/accounts/does-not-exist")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_list_accounts_returns_created_accounts(client):
    await client.post("/api/accounts", json={"phone": "+821077778888"})
    await client.post("/api/accounts", json={"phone": "+821099990000"})
    res = await client.get("/api/accounts")
    assert res.status_code == 200
    body = res.json()
    assert len(body["items"]) == 2
    assert body["total"] == 2


@pytest.mark.asyncio
async def test_search_accounts_by_phone(client):
    await client.post("/api/accounts", json={"phone": "+821011111111", "name": "Alice"})
    await client.post("/api/accounts", json={"phone": "+821022222222", "name": "Bob"})
    res = await client.get("/api/accounts?search=Alice")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Alice"


@pytest.mark.asyncio
async def test_filter_accounts_by_status(client):
    a1 = await client.post("/api/accounts", json={"phone": "+821033333333"})
    await client.put(f"/api/accounts/{a1.json()['id']}", json={"status": "active"})
    await client.post("/api/accounts", json={"phone": "+821044444444"})
    res = await client.get("/api/accounts?status=active")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] >= 1
    for item in body["items"]:
        assert item["status"] == "active"


@pytest.mark.asyncio
async def test_paginated_accounts(client):
    for i in range(5):
        await client.post("/api/accounts", json={"phone": f"+8210{i:06d}0000"})
    res = await client.get("/api/accounts?page=1&page_size=2")
    assert res.status_code == 200
    body = res.json()
    assert len(body["items"]) == 2
    assert body["total"] >= 5
    assert body["total_pages"] >= 3


@pytest.mark.asyncio
async def test_account_summary(client):
    res = await client.get("/api/accounts/summary")
    assert res.status_code == 200
    body = res.json()
    assert "total" in body
    assert "healthy" in body
    assert "unhealthy" in body
    assert "active_accounts" in body
    assert "inactive_accounts" in body


@pytest.mark.asyncio
async def test_bulk_activate_accounts(client):
    a1 = await client.post("/api/accounts", json={"phone": "+821055555555"})
    a2 = await client.post("/api/accounts", json={"phone": "+821066666666"})
    res = await client.post("/api/accounts/bulk", json={
        "account_ids": [a1.json()["id"], a2.json()["id"]],
        "action": "activate",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["total_processed"] == 2
    assert body["total_failed"] == 0


@pytest.mark.asyncio
async def test_bulk_deactivate_accounts(client):
    a1 = await client.post("/api/accounts", json={"phone": "+821077777777"})
    res = await client.post("/api/accounts/bulk", json={
        "account_ids": [a1.json()["id"]],
        "action": "deactivate",
    })
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_bulk_reset_session(client):
    a1 = await client.post("/api/accounts", json={"phone": "+821088888888"})
    res = await client.post("/api/accounts/bulk", json={
        "account_ids": [a1.json()["id"]],
        "action": "reset_session",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["total_processed"] == 1


@pytest.mark.asyncio
async def test_bulk_delete_accounts(client):
    a1 = await client.post("/api/accounts", json={"phone": "+821099999999"})
    res = await client.post("/api/accounts/bulk", json={
        "account_ids": [a1.json()["id"]],
        "action": "delete",
    })
    assert res.status_code == 200
    get_back = await client.get(f"/api/accounts/{a1.json()['id']}")
    assert get_back.status_code == 404


@pytest.mark.asyncio
async def test_bulk_unknown_action(client):
    a1 = await client.post("/api/accounts", json={"phone": "+821000000001"})
    res = await client.post("/api/accounts/bulk", json={
        "account_ids": [a1.json()["id"]],
        "action": "fly_to_moon",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["total_failed"] == 1


@pytest.mark.asyncio
async def test_sort_accounts_by_phone(client):
    await client.post("/api/accounts", json={"phone": "+8210AAAAAA", "name": "Z"})
    await client.post("/api/accounts", json={"phone": "+8210BBBBBB", "name": "A"})
    res = await client.get("/api/accounts?sort_by=phone&sort_dir=asc&page_size=100")
    body = res.json()
    phones = [item["phone"] for item in body["items"]]
    assert phones == sorted(phones)


@pytest.mark.asyncio
async def test_resume_suspended_account_returns_active(client, db_session):
    from app.api.deps import require_admin
    from app.models.account import Account
    from sqlalchemy import select

    app.dependency_overrides[require_admin] = lambda: None
    try:
        created = await client.post("/api/accounts", json={"phone": "+821099990000", "name": "재개 테스트"})
        account_id = created.json()["id"]

        result = await db_session.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
        assert account is not None
        account.status = "suspended"
        account.last_error = "규제 의심"
        account.last_error_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None)
        await db_session.commit()

        res = await client.post(f"/api/accounts/{account_id}/resume")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "active"
        assert body["last_error"] is None
        assert body["last_error_at"] is None

        await db_session.refresh(account)
        assert account.status == "active"
    finally:
        app.dependency_overrides.pop(require_admin, None)


@pytest.mark.asyncio
async def test_resume_non_suspended_account_returns_400(client, db_session):
    from app.api.deps import require_admin

    app.dependency_overrides[require_admin] = lambda: None
    try:
        created = await client.post("/api/accounts", json={"phone": "+821099990001", "name": "재개 불가 테스트"})
        account_id = created.json()["id"]

        res = await client.post(f"/api/accounts/{account_id}/resume")
        assert res.status_code == 400
        assert "일시중단" in res.json()["detail"]
    finally:
        app.dependency_overrides.pop(require_admin, None)
