import pytest


@pytest.mark.asyncio
async def test_list_accounts_empty(client):
    res = await client.get("/api/accounts")
    assert res.status_code == 200
    assert res.json() == []


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
    assert len(res.json()) == 2
