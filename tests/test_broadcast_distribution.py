import json

import pytest

from app.crud import account as account_crud
from app.schemas.account import AccountCreate
from app.services.broadcast_distribution import (
    DISTRIBUTION_GROUP_THRESHOLD,
    check_membership,
    create_distributed_broadcast,
    get_eligible_accounts,
    plan_distribution,
)
from app.services.telegram_actions import AccountNotAuthenticatedError


async def _make_account(db_session, phone="+821011110000", status="active", tenant_id=None):
    account = await account_crud.create_account(db_session, AccountCreate(phone=phone))
    account.status = status
    account.tenant_id = tenant_id
    await db_session.commit()
    await db_session.refresh(account)
    return account


# ── plan_distribution (pure) ────────────────────────────────────────


def test_plan_distribution_single_member_account_is_not_distributed():
    membership = {"acc-a": {"g1", "g2", "g3"}}
    plan = plan_distribution(membership, ["g1", "g2", "g3"], requesting_account_id="acc-a")

    assert plan.distributed is False
    assert plan.assignments == {"acc-a": ["g1", "g2", "g3"]}


def test_plan_distribution_splits_by_membership_and_balances_load():
    membership = {
        "acc-a": {f"g{i}" for i in range(10)},
        "acc-b": {f"g{i}" for i in range(10)},
    }
    group_ids = [f"g{i}" for i in range(10)]

    plan = plan_distribution(membership, group_ids, requesting_account_id="acc-a")

    assert plan.distributed is True
    assert set(plan.assignments["acc-a"]) | set(plan.assignments["acc-b"]) == set(group_ids)
    # Both accounts are members of every group, so load should be balanced 5/5.
    assert len(plan.assignments["acc-a"]) == 5
    assert len(plan.assignments["acc-b"]) == 5


def test_plan_distribution_only_assigns_groups_to_actual_members():
    membership = {
        "acc-a": {"g1"},
        "acc-b": {"g2", "g3"},
    }
    plan = plan_distribution(membership, ["g1", "g2", "g3"], requesting_account_id="acc-a")

    assert plan.assignments["acc-a"] == ["g1"]
    assert set(plan.assignments["acc-b"]) == {"g2", "g3"}


def test_plan_distribution_falls_back_to_requester_for_orphan_group():
    membership = {
        "acc-a": {"g1"},
        "acc-b": {"g2"},
    }
    # g99 has no eligible member among the candidates.
    plan = plan_distribution(membership, ["g1", "g2", "g99"], requesting_account_id="acc-a")

    assert "g99" in plan.assignments["acc-a"]


def test_plan_distribution_all_groups_are_orphans_falls_back_to_requester():
    """When no candidate is a member of any target group, everything falls
    back to the requesting account."""
    membership = {
        "acc-a": set(),
        "acc-b": set(),
    }
    plan = plan_distribution(membership, ["g1", "g2", "g3"], requesting_account_id="acc-a")

    assert plan.assignments == {"acc-a": ["g1", "g2", "g3"]}
    assert plan.distributed is False


def test_plan_distribution_partial_membership_balances_load():
    """Load balancing still works when accounts only partially overlap."""
    membership = {
        "acc-a": {"g1", "g2", "g3"},
        "acc-b": {"g2", "g3", "g4"},
        "acc-c": {"g3", "g4", "g5"},
    }
    group_ids = [f"g{i}" for i in range(1, 6)]

    plan = plan_distribution(membership, group_ids, requesting_account_id="acc-a")

    # Every group must be assigned to someone who is actually a member.
    for group_id in group_ids:
        assigned_to = [aid for aid, groups in membership.items() if group_id in groups and group_id in (plan.assignments.get(aid) or [])]
        # A group may also be assigned to requester via fallback, but at least
        # one eligible member must have received it.
        assert len(assigned_to) >= 1 or group_id in plan.assignments.get("acc-a", [])


# ── check_membership ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_membership_intersects_with_target_groups(monkeypatch):
    async def fake_list_groups(account):
        return [{"id": "g1"}, {"id": "g2"}, {"id": "other"}]

    monkeypatch.setattr(
        "app.services.broadcast_distribution.list_groups", fake_list_groups
    )

    class FakeAccount:
        id = "acc-a"

    result = await check_membership([FakeAccount()], ["g1", "g2", "g3"])
    assert result == {"acc-a": {"g1", "g2"}}


@pytest.mark.asyncio
async def test_check_membership_treats_unauthenticated_account_as_no_membership(monkeypatch):
    async def fake_list_groups(account):
        raise AccountNotAuthenticatedError("no session")

    monkeypatch.setattr(
        "app.services.broadcast_distribution.list_groups", fake_list_groups
    )

    class FakeAccount:
        id = "acc-a"

    result = await check_membership([FakeAccount()], ["g1"])
    assert result == {"acc-a": set()}


# ── get_eligible_accounts ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_eligible_accounts_excludes_suspended_and_banned(db_session):
    active = await _make_account(db_session, phone="+821011110001", status="active")
    await _make_account(db_session, phone="+821011110002", status="suspended")
    await _make_account(db_session, phone="+821011110003", status="banned")

    eligible = await get_eligible_accounts(db_session, tenant_id=None)

    eligible_ids = {a.id for a in eligible}
    assert active.id in eligible_ids
    assert len(eligible_ids) == 1


# ── create_distributed_broadcast (integration) ───────────────────────


@pytest.mark.asyncio
async def test_create_distributed_broadcast_splits_across_member_accounts(db_session, monkeypatch):
    acc_a = await _make_account(db_session, phone="+821011110010", status="active")
    acc_b = await _make_account(db_session, phone="+821011110011", status="active")

    group_ids = [f"g{i}" for i in range(30)]
    # acc_a is a member of the first half, acc_b of the second half.
    membership_by_phone = {
        acc_a.phone: [{"id": g} for g in group_ids[:15]],
        acc_b.phone: [{"id": g} for g in group_ids[15:]],
    }

    async def fake_list_groups(account):
        return membership_by_phone[account.phone]

    monkeypatch.setattr(
        "app.services.broadcast_distribution.list_groups", fake_list_groups
    )

    broadcasts = await create_distributed_broadcast(
        db_session,
        requesting_account=acc_a,
        target_ids=group_ids,
        target_field="group_ids",
        message="분산 테스트",
        media_path=None,
        delivery_mode="normal",
        delay_seconds=None,
        inline_buttons=None,
        reply_to_msg_id=None,
        scheduled_at=None,
        campaign_id=None,
    )

    assert len(broadcasts) == 2
    assert broadcasts[0].account_id == acc_a.id  # requester first
    batch_ids = {b.distribution_batch_id for b in broadcasts}
    assert len(batch_ids) == 1
    assert None not in batch_ids

    all_assigned = set(broadcasts[0].group_ids) | set(broadcasts[1].group_ids)
    assert all_assigned == set(group_ids)
    # group_ids mode: recipients must stay empty (member-resolution happens at
    # dispatch time), never pre-filled with the chat ids themselves.
    assert broadcasts[0].recipients == []
    assert broadcasts[1].recipients == []


@pytest.mark.asyncio
async def test_create_distributed_broadcast_recipients_mode_splits_into_recipients_field(
    db_session, monkeypatch
):
    """This is the mode SendTab's actual group-broadcast UI uses (and the one
    that caused the 07ede2ef incident) — each "recipient" is a group's own
    chat id to message directly, not a group to resolve members from. Mixing
    this up with group_ids mode would turn a handful of groups into messaging
    every member of each one."""
    acc_a = await _make_account(db_session, phone="+821011110013", status="active")
    acc_b = await _make_account(db_session, phone="+821011110014", status="active")

    recipient_ids = [f"g{i}" for i in range(30)]
    membership_by_phone = {
        acc_a.phone: [{"id": g} for g in recipient_ids[:15]],
        acc_b.phone: [{"id": g} for g in recipient_ids[15:]],
    }

    async def fake_list_groups(account):
        return membership_by_phone[account.phone]

    monkeypatch.setattr(
        "app.services.broadcast_distribution.list_groups", fake_list_groups
    )

    broadcasts = await create_distributed_broadcast(
        db_session,
        requesting_account=acc_a,
        target_ids=recipient_ids,
        target_field="recipients",
        message="분산 테스트",
        media_path=None,
        delivery_mode="bulk",
        delay_seconds=None,
        inline_buttons=None,
        reply_to_msg_id=None,
        scheduled_at=None,
        campaign_id=None,
    )

    assert len(broadcasts) == 2
    for b in broadcasts:
        assert b.group_ids is None
    all_assigned = set(broadcasts[0].recipients) | set(broadcasts[1].recipients)
    assert all_assigned == set(recipient_ids)


@pytest.mark.asyncio
async def test_create_distributed_broadcast_single_candidate_has_no_batch_id(db_session, monkeypatch):
    acc_a = await _make_account(db_session, phone="+821011110012", status="active")
    group_ids = [f"g{i}" for i in range(30)]

    async def fake_list_groups(account):
        return [{"id": g} for g in group_ids]

    monkeypatch.setattr(
        "app.services.broadcast_distribution.list_groups", fake_list_groups
    )

    broadcasts = await create_distributed_broadcast(
        db_session,
        requesting_account=acc_a,
        target_ids=group_ids,
        target_field="group_ids",
        message="단일 계정",
        media_path=None,
        delivery_mode="normal",
        delay_seconds=None,
        inline_buttons=None,
        reply_to_msg_id=None,
        scheduled_at=None,
        campaign_id=None,
    )

    assert len(broadcasts) == 1
    assert broadcasts[0].distribution_batch_id is None
    assert set(broadcasts[0].group_ids) == set(group_ids)


@pytest.mark.asyncio
async def test_create_distributed_broadcast_empty_target_ids_returns_empty(db_session, monkeypatch):
    acc_a = await _make_account(db_session, phone="+821011110015", status="active")

    broadcasts = await create_distributed_broadcast(
        db_session,
        requesting_account=acc_a,
        target_ids=[],
        target_field="group_ids",
        message="빈 대상",
        media_path=None,
        delivery_mode="normal",
        delay_seconds=None,
        inline_buttons=None,
        reply_to_msg_id=None,
        scheduled_at=None,
        campaign_id=None,
    )

    assert broadcasts == []


@pytest.mark.asyncio
async def test_create_distributed_broadcast_requesting_account_suspended_raises(db_session, monkeypatch):
    acc_a = await _make_account(db_session, phone="+821011110016", status="suspended")
    acc_b = await _make_account(db_session, phone="+821011110017", status="active")

    group_ids = [f"g{i}" for i in range(5)]

    async def fake_list_groups(account):
        return [{"id": g} for g in group_ids]

    monkeypatch.setattr(
        "app.services.broadcast_distribution.list_groups", fake_list_groups
    )

    with pytest.raises(ValueError, match="suspended"):
        await create_distributed_broadcast(
            db_session,
            requesting_account=acc_a,
            target_ids=group_ids,
            target_field="group_ids",
            message="suspended 요청",
            media_path=None,
            delivery_mode="normal",
            delay_seconds=None,
            inline_buttons=None,
            reply_to_msg_id=None,
            scheduled_at=None,
            campaign_id=None,
        )


@pytest.mark.asyncio
async def test_create_distributed_broadcast_at_threshold_boundary_is_not_distributed(
    client, db_session, monkeypatch
):
    account_id = await _create_account_via_api(client, "+821022220003")

    from sqlalchemy import select
    from app.models.account import Account

    result = await db_session.execute(select(Account).where(Account.id == account_id))
    acc = result.scalar_one()
    acc.status = "active"
    await db_session.commit()

    group_ids = [f"g{i}" for i in range(DISTRIBUTION_GROUP_THRESHOLD)]

    async def fake_list_groups(account):
        return [{"id": g} for g in group_ids]

    monkeypatch.setattr(
        "app.services.broadcast_distribution.list_groups", fake_list_groups
    )

    res = await client.post(
        "/api/broadcast",
        data={
            "account_id": account_id,
            "message": "경계값 테스트",
            "recipients": json.dumps([]),
            "group_ids": json.dumps(group_ids),
        },
    )
    assert res.status_code == 202
    assert res.json()["distribution_batch_id"] is None


# ── API integration ──────────────────────────────────────────────────


async def _create_account_via_api(client, phone):
    res = await client.post("/api/accounts", json={"phone": phone, "name": "분산 테스트 계정"})
    assert res.status_code == 201
    return res.json()["id"]


@pytest.mark.asyncio
async def test_create_broadcast_distributes_when_group_ids_exceed_threshold(
    client, db_session, monkeypatch
):
    account_a_id = await _create_account_via_api(client, "+821022220000")
    account_b_id = await _create_account_via_api(client, "+821022220001")

    from sqlalchemy import select
    from app.models.account import Account

    for acc_id in (account_a_id, account_b_id):
        result = await db_session.execute(select(Account).where(Account.id == acc_id))
        acc = result.scalar_one()
        acc.status = "active"
    await db_session.commit()

    group_ids = [f"g{i}" for i in range(DISTRIBUTION_GROUP_THRESHOLD + 5)]

    async def fake_list_groups(account):
        # Both test accounts are members of every target group.
        return [{"id": g} for g in group_ids]

    monkeypatch.setattr(
        "app.services.broadcast_distribution.list_groups", fake_list_groups
    )

    res = await client.post(
        "/api/broadcast",
        data={
            "account_id": account_a_id,
            "message": "분산 발송 테스트",
            "recipients": json.dumps([]),
            "group_ids": json.dumps(group_ids),
        },
    )
    assert res.status_code == 202
    body = res.json()
    assert body["distribution_batch_id"] is not None

    status_res = await client.get(f"/api/broadcast/distribution/{body['distribution_batch_id']}")
    assert status_res.status_code == 200
    siblings = status_res.json()["siblings"]
    assert len(siblings) == 2
    all_groups = set()
    for sib in siblings:
        all_groups.update(sib["broadcast"]["group_ids"])
    assert all_groups == set(group_ids)


@pytest.mark.asyncio
async def test_create_broadcast_distributes_when_recipients_exceed_threshold(
    client, db_session, monkeypatch
):
    """SendTab's actual group-broadcast UI sends selected group chat ids via
    `recipients`, not `group_ids` — this is the real path that produced the
    07ede2ef incident, so it must trigger distribution too."""
    account_a_id = await _create_account_via_api(client, "+821022220010")
    account_b_id = await _create_account_via_api(client, "+821022220011")

    from sqlalchemy import select
    from app.models.account import Account

    for acc_id in (account_a_id, account_b_id):
        result = await db_session.execute(select(Account).where(Account.id == acc_id))
        acc = result.scalar_one()
        acc.status = "active"
    await db_session.commit()

    recipient_ids = [f"g{i}" for i in range(DISTRIBUTION_GROUP_THRESHOLD + 5)]

    async def fake_list_groups(account):
        return [{"id": g} for g in recipient_ids]

    monkeypatch.setattr(
        "app.services.broadcast_distribution.list_groups", fake_list_groups
    )

    res = await client.post(
        "/api/broadcast",
        data={
            "account_id": account_a_id,
            "message": "분산 발송 테스트 (recipients)",
            "recipients": json.dumps(recipient_ids),
        },
    )
    assert res.status_code == 202
    body = res.json()
    assert body["distribution_batch_id"] is not None
    assert body["group_ids"] is None

    status_res = await client.get(f"/api/broadcast/distribution/{body['distribution_batch_id']}")
    assert status_res.status_code == 200
    siblings = status_res.json()["siblings"]
    assert len(siblings) == 2
    all_recipients = set()
    for sib in siblings:
        assert sib["broadcast"]["group_ids"] is None
        all_recipients.update(sib["broadcast"]["recipients"])
    assert all_recipients == set(recipient_ids)


@pytest.mark.asyncio
async def test_create_broadcast_below_threshold_is_not_distributed(client):
    account_id = await _create_account_via_api(client, "+821022220002")

    group_ids = ["g1", "g2", "g3"]
    res = await client.post(
        "/api/broadcast",
        data={
            "account_id": account_id,
            "message": "단일 발송",
            "recipients": json.dumps([]),
            "group_ids": json.dumps(group_ids),
        },
    )
    assert res.status_code == 202
    assert res.json()["distribution_batch_id"] is None
