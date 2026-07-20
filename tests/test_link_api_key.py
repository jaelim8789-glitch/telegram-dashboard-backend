import pytest

from app.api.deps import Identity
from app.core.security import generate_api_key
from app.crud import api_key as api_key_crud
from app.models.api_key import APIKey
from app.models.tenant import Tenant
from app.models.user import User
from sqlalchemy import select


def _identity(tenant_id: str | None = None, kind: str = "user", api_key_id: str | None = None):
    return Identity(kind=kind, tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_link_api_key_requires_auth(unauthenticated_client):
    res = await unauthenticated_client.post("/api/auth/link-api-key", json={"key": "sk-abcdef"})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_link_api_key_requires_tenant(unauthenticated_client, db_session):
    from app.main import app
    import app.database as db_mod
    from app.api.auth import get_current_identity as auth_get_current_identity

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[db_mod.get_db] = _override_get_db
    app.dependency_overrides[auth_get_current_identity] = lambda: _identity(tenant_id=None)

    try:
        res = await unauthenticated_client.post("/api/auth/link-api-key", json={"key": "sk-abcdef"})
        assert res.status_code == 403
        assert res.json()["detail"] == "이 기능에 접근할 수 없습니다. 먼저 결제/요금제를 설정해주세요."
    finally:
        app.dependency_overrides.pop(db_mod.get_db, None)
        app.dependency_overrides.pop(auth_get_current_identity, None)


@pytest.mark.asyncio
async def test_link_invalid_format_rejected(unauthenticated_client, db_session):
    from app.main import app
    import app.database as db_mod
    from app.api.auth import get_current_identity as auth_get_current_identity

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[db_mod.get_db] = _override_get_db
    app.dependency_overrides[auth_get_current_identity] = lambda: _identity(tenant_id="tenant-1")

    try:
        for bad in ["not-a-key", "sk-", "sk-abc", "hello-world"]:
            res = await unauthenticated_client.post("/api/auth/link-api-key", json={"key": bad})
            assert res.status_code == 400, f"expected 400 for {bad!r}, got {res.status_code}"
    finally:
        app.dependency_overrides.pop(db_mod.get_db, None)
        app.dependency_overrides.pop(auth_get_current_identity, None)


@pytest.mark.asyncio
async def test_link_existing_unlinked_key(unauthenticated_client, db_session):
    from app.main import app
    import app.database as db_mod
    from app.api.auth import get_current_identity as auth_get_current_identity

    phone = "+821099991101"
    user = User(phone=phone)
    db_session.add(user)
    tenant = Tenant(phone=phone, plan="pro", subscription_status="active")
    db_session.add(tenant)
    await db_session.flush()
    await db_session.commit()
    await db_session.refresh(user)
    await db_session.refresh(tenant)

    raw_key = generate_api_key()
    existing = APIKey(key=raw_key, name="미연결 키", tenant_id=None, is_active=True)
    db_session.add(existing)
    await db_session.commit()
    await db_session.refresh(existing)

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[db_mod.get_db] = _override_get_db
    app.dependency_overrides[auth_get_current_identity] = lambda: _identity(tenant_id=tenant.id)

    try:
        res = await unauthenticated_client.post("/api/auth/link-api-key", json={"key": raw_key})
        assert res.status_code == 200
        body = res.json()
        assert body["tenant_id"] == tenant.id
        assert body["name"] == "미연결 키"
    finally:
        app.dependency_overrides.pop(db_mod.get_db, None)
        app.dependency_overrides.pop(auth_get_current_identity, None)


@pytest.mark.asyncio
async def test_link_same_tenant_is_idempotent(unauthenticated_client, db_session):
    from app.main import app
    import app.database as db_mod
    from app.api.auth import get_current_identity as auth_get_current_identity

    phone = "+821099991102"
    user = User(phone=phone)
    db_session.add(user)
    tenant = Tenant(phone=phone, plan="pro", subscription_status="active")
    db_session.add(tenant)
    await db_session.flush()
    await db_session.commit()
    await db_session.refresh(user)
    await db_session.refresh(tenant)

    raw_key = generate_api_key()
    existing = APIKey(key=raw_key, name="이미내키", tenant_id=tenant.id, is_active=True)
    db_session.add(existing)
    await db_session.commit()

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[db_mod.get_db] = _override_get_db
    app.dependency_overrides[auth_get_current_identity] = lambda: _identity(tenant_id=tenant.id)

    try:
        res = await unauthenticated_client.post("/api/auth/link-api-key", json={"key": raw_key})
        assert res.status_code == 200
        assert res.json()["tenant_id"] == tenant.id
    finally:
        app.dependency_overrides.pop(db_mod.get_db, None)
        app.dependency_overrides.pop(auth_get_current_identity, None)


@pytest.mark.asyncio
async def test_link_key_conflict_when_already_linked(unauthenticated_client, db_session):
    from app.main import app
    import app.database as db_mod
    from app.api.auth import get_current_identity as auth_get_current_identity

    phone = "+821099991103"
    user = User(phone=phone)
    db_session.add(user)
    tenant_a = Tenant(phone=phone, plan="pro", subscription_status="active")
    tenant_b = Tenant(phone=f"+821099991104", plan="pro", subscription_status="active")
    db_session.add(tenant_a)
    db_session.add(tenant_b)
    await db_session.flush()
    await db_session.commit()
    await db_session.refresh(user)
    await db_session.refresh(tenant_a)
    await db_session.refresh(tenant_b)

    raw_key = generate_api_key()
    existing = APIKey(key=raw_key, name="이미연결", tenant_id=tenant_b.id, is_active=True)
    db_session.add(existing)
    await db_session.commit()

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[db_mod.get_db] = _override_get_db
    app.dependency_overrides[auth_get_current_identity] = lambda: _identity(tenant_id=tenant_a.id)

    try:
        res = await unauthenticated_client.post("/api/auth/link-api-key", json={"key": raw_key})
        assert res.status_code == 409
        assert "다른 테넌트" in res.json()["detail"]
    finally:
        app.dependency_overrides.pop(db_mod.get_db, None)
        app.dependency_overrides.pop(auth_get_current_identity, None)


@pytest.mark.asyncio
async def test_link_admin_managed_key_rejected(unauthenticated_client, db_session):
    from app.main import app
    import app.database as db_mod
    from app.api.auth import get_current_identity as auth_get_current_identity

    phone = "+821099991107"
    user = User(phone=phone)
    db_session.add(user)
    tenant = Tenant(phone=phone, plan="pro", subscription_status="active")
    db_session.add(tenant)
    await db_session.flush()
    await db_session.commit()
    await db_session.refresh(user)
    await db_session.refresh(tenant)

    raw_key = generate_api_key()
    existing = APIKey(key=raw_key, name="관리자발급", tenant_id=None, is_active=True, purpose="admin_managed")
    db_session.add(existing)
    await db_session.commit()

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[db_mod.get_db] = _override_get_db
    app.dependency_overrides[auth_get_current_identity] = lambda: _identity(tenant_id=tenant.id)

    try:
        res = await unauthenticated_client.post("/api/auth/link-api-key", json={"key": raw_key})
        assert res.status_code == 403
        assert "관리자" in res.json()["detail"]
    finally:
        app.dependency_overrides.pop(db_mod.get_db, None)
        app.dependency_overrides.pop(auth_get_current_identity, None)


@pytest.mark.asyncio
async def test_broadcast_gate_blocks_without_linked_key(unauthenticated_client, db_session):
    from app.main import app
    import app.database as db_mod
    from app.api.auth import get_current_identity as auth_get_current_identity
    from app.api.deps import require_api_key_or_admin
    from app.models.account import Account
    from app.services.usage_tracker import apply_plan_limits

    phone = "+821099991105"
    user = User(phone=phone)
    db_session.add(user)
    tenant = Tenant(phone=phone, plan="pro", subscription_status="active")
    db_session.add(tenant)
    await db_session.flush()
    await apply_plan_limits(db_session, tenant, "pro")
    await db_session.refresh(tenant)

    account = Account(tenant_id=tenant.id, phone=phone, name="테스트계정", status="active")
    db_session.add(account)
    await db_session.commit()
    await db_session.refresh(account)

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[db_mod.get_db] = _override_get_db
    app.dependency_overrides[require_api_key_or_admin] = lambda: None
    app.dependency_overrides[auth_get_current_identity] = lambda: _identity(tenant_id=tenant.id)

    try:
        res = await unauthenticated_client.post(
            "/api/broadcast",
            data={"account_id": account.id, "message": "hello", "recipients": "[]"},
            headers={"X-Session-Token": "dummy"},
        )
        assert res.status_code == 403
        assert "API키가 필요합니다" in res.json()["detail"]
    finally:
        app.dependency_overrides.pop(db_mod.get_db, None)
        app.dependency_overrides.pop(require_api_key_or_admin, None)
        app.dependency_overrides.pop(auth_get_current_identity, None)


@pytest.mark.asyncio
async def test_broadcast_allows_with_linked_key(unauthenticated_client, db_session):
    from app.main import app
    import app.database as db_mod
    from app.api.auth import get_current_identity as auth_get_current_identity
    from app.api.deps import require_api_key_or_admin
    from app.models.account import Account
    from app.services.usage_tracker import apply_plan_limits

    phone = "+821099991106"
    user = User(phone=phone)
    db_session.add(user)
    tenant = Tenant(phone=phone, plan="pro", subscription_status="active")
    db_session.add(tenant)
    await db_session.flush()
    await apply_plan_limits(db_session, tenant, "pro")
    await db_session.refresh(tenant)

    account = Account(tenant_id=tenant.id, phone=phone, name="테스트계정2", status="active")
    db_session.add(account)

    raw_key = generate_api_key()
    linked = APIKey(key=raw_key, name="연결됨", tenant_id=None, is_active=True)
    db_session.add(linked)
    await db_session.commit()
    await db_session.refresh(account)
    await db_session.refresh(linked)

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[db_mod.get_db] = _override_get_db
    app.dependency_overrides[require_api_key_or_admin] = lambda: None
    app.dependency_overrides[auth_get_current_identity] = lambda: _identity(tenant_id=tenant.id, kind="api_key")

    try:
        res = await unauthenticated_client.post(
            "/api/auth/link-api-key",
            json={"key": raw_key},
        )
        assert res.status_code == 200
        assert res.json()["tenant_id"] == tenant.id

        res = await unauthenticated_client.post(
            "/api/broadcast",
            data={"account_id": account.id, "message": "hello", "recipients": "[]"},
            headers={"X-API-Key": raw_key},
        )
        assert res.status_code != 403
    finally:
        app.dependency_overrides.pop(db_mod.get_db, None)
        app.dependency_overrides.pop(require_api_key_or_admin, None)
        app.dependency_overrides.pop(auth_get_current_identity, None)
