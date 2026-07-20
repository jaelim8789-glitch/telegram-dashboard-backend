import json
from datetime import datetime, timedelta, timezone

import pytest


async def _create_account(client, phone="+821012340000"):
    res = await client.post("/api/accounts", json={"phone": phone, "name": "발송 테스트 계정"})
    assert res.status_code == 201
    return res.json()["id"]


def _broadcast_form(account_id, message="안녕하세요", recipients=None, scheduled_at=None):
    data = {
        "account_id": account_id,
        "message": message,
        "recipients": json.dumps(recipients or ["-100111"]),
    }
    if scheduled_at:
        data["scheduled_at"] = scheduled_at
    return data


@pytest.mark.asyncio
async def test_create_broadcast_immediate(client):
    account_id = await _create_account(client)

    res = await client.post("/api/broadcast", data=_broadcast_form(account_id))
    assert res.status_code == 202
    body = res.json()
    assert body["account_id"] == account_id
    assert body["status"] == "pending"
    assert body["scheduled_at"] is None
    assert body["recipients"] == ["-100111"]


@pytest.mark.asyncio
async def test_create_broadcast_persists_delay_seconds(client):
    """Production symptom: the frontend's '일반 발송 간격' selector (5/10/30/60초)
    sent delay_seconds, but create_broadcast had no such Form parameter — it
    was silently dropped and the selector had zero effect."""
    account_id = await _create_account(client)
    data = _broadcast_form(account_id)
    data["delay_seconds"] = "10"
    res = await client.post("/api/broadcast", data=data)
    assert res.status_code == 202, res.text
    assert res.json()["delay_seconds"] == 10


@pytest.mark.asyncio
async def test_create_broadcast_without_delay_seconds_defaults_to_none(client):
    account_id = await _create_account(client)
    res = await client.post("/api/broadcast", data=_broadcast_form(account_id))
    assert res.status_code == 202
    assert res.json()["delay_seconds"] is None


@pytest.mark.asyncio
async def test_create_broadcast_allows_more_than_ten_recipients(client):
    """Pre-existing stale test updated: the 10-recipient hard cap was removed
    from app/core/limits.py in an earlier, unrelated change ("무제한 발송") —
    this suite still asserted the old 422-at-11 behavior. Not part of the
    13-problem recovery scope; fixed here only because it shares this file
    with the delay_seconds tests above and was failing in the full test run."""
    account_id = await _create_account(client)

    recipients = [str(i) for i in range(11)]
    res = await client.post("/api/broadcast", data=_broadcast_form(account_id, recipients=recipients))
    assert res.status_code == 202


@pytest.mark.asyncio
async def test_create_broadcast_invalid_recipients_json(client):
    account_id = await _create_account(client)

    res = await client.post(
        "/api/broadcast",
        data={"account_id": account_id, "message": "hi", "recipients": "not-json"},
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_create_broadcast_unknown_account(client):
    res = await client.post("/api/broadcast", data=_broadcast_form("does-not-exist"))
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_create_broadcast_rate_limited(client):
    account_id = await _create_account(client)

    first = await client.post("/api/broadcast", data=_broadcast_form(account_id))
    assert first.status_code == 202

    second = await client.post("/api/broadcast", data=_broadcast_form(account_id, message="다시"))
    assert second.status_code == 429
    assert "1분에 1회" in second.json()["detail"]


@pytest.mark.asyncio
async def test_create_broadcast_scheduled_is_not_enqueued_immediately(client):
    account_id = await _create_account(client)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    res = await client.post("/api/broadcast", data=_broadcast_form(account_id, scheduled_at=future))
    assert res.status_code == 202
    body = res.json()
    assert body["status"] == "pending"
    assert body["scheduled_at"] is not None

    upcoming = await client.get("/api/scheduler/upcoming")
    assert upcoming.status_code == 200
    assert any(item["id"] == body["id"] for item in upcoming.json())


@pytest.mark.asyncio
async def test_scheduled_broadcast_does_not_count_against_rate_limit(client):
    """A far-future scheduled broadcast must not block an immediate send for the same account."""
    account_id = await _create_account(client)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    scheduled = await client.post("/api/broadcast", data=_broadcast_form(account_id, scheduled_at=future))
    assert scheduled.status_code == 202

    immediate = await client.post("/api/broadcast", data=_broadcast_form(account_id, message="즉시"))
    assert immediate.status_code == 202


@pytest.mark.asyncio
async def test_get_broadcast(client):
    account_id = await _create_account(client)
    created = await client.post("/api/broadcast", data=_broadcast_form(account_id))
    broadcast_id = created.json()["id"]

    res = await client.get(f"/api/broadcast/{broadcast_id}")
    assert res.status_code == 200
    assert res.json()["id"] == broadcast_id


@pytest.mark.asyncio
async def test_get_broadcast_not_found(client):
    res = await client.get("/api/broadcast/does-not-exist")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_logs_filter_by_account_and_status(client):
    account_id = await _create_account(client)
    await client.post("/api/broadcast", data=_broadcast_form(account_id))

    by_account = await client.get(f"/api/logs?account_id={account_id}")
    assert by_account.status_code == 200
    assert len(by_account.json()) == 1

    by_status = await client.get("/api/logs?status=pending")
    assert by_status.status_code == 200
    assert all(item["status"] == "pending" for item in by_status.json())

    by_missing_account = await client.get("/api/logs?account_id=does-not-exist")
    assert by_missing_account.status_code == 200
    assert by_missing_account.json() == []


@pytest.mark.asyncio
async def test_logs_tenant_isolation(client):
    """A tenant user must not see logs for another tenant's account (403)."""
    from app.api.deps import get_current_identity, Identity
    from app.main import app

    account_id = await _create_account(client)
    await client.post("/api/broadcast", data=_broadcast_form(account_id))

    # Override identity to a different tenant
    app.dependency_overrides[get_current_identity] = lambda: Identity(kind="user", tenant_id="other-tenant")
    try:
        resp = await client.get(f"/api/logs?account_id={account_id}")
        assert resp.status_code == 403
        assert "접근" in resp.json()["detail"] or "권한" in resp.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_current_identity, None)


@pytest.mark.asyncio
async def test_create_broadcast_suspended_account_returns_403(client, db_session):
    account_id = await _create_account(client)

    from sqlalchemy import select
    from app.models.account import Account

    result = await db_session.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()
    assert account is not None
    account.status = "suspended"
    await db_session.commit()

    res = await client.post("/api/broadcast", data=_broadcast_form(account_id))
    assert res.status_code == 403
    assert "일시 중단" in res.json()["detail"]


@pytest.mark.asyncio
async def test_send_to_group_suspended_account_returns_403(client, db_session):
    account_id = await _create_account(client)

    from sqlalchemy import select
    from app.models.account import Account

    result = await db_session.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()
    assert account is not None
    account.status = "suspended"
    await db_session.commit()

    res = await client.post(
        "/api/broadcast/send-group",
        json={"account_id": account_id, "message": "hi", "group_ids": ["-100123"]},
    )
    assert res.status_code == 403
    assert "일시 중단" in res.json()["detail"]
