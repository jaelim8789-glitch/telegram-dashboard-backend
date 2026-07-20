"""Multi-account group distribution for broadcasts.

Why this exists: a single account blasting immediate messages into many groups
at once is exactly the pattern that got account 07ede2ef flagged by Telegram
(forbidden across all 11 of its groups within days). The fix is not a more
aggressive send loop on one account — it's spreading the same group list
across the tenant's other active accounts so each one sends at the existing,
already-safe pace (see app/services/delivery.py pacing / app/services/
broadcast_processor.py) but only has to carry its own slice.

A Telegram chat is only writable by an account that is actually a member of
it — there is no persisted "which account is in which group" table (group
lists are always fetched live via Telethon, see app/services/
telegram_actions.list_groups). So distribution has to verify live membership
per candidate account before it can assign a group to it; assigning blind
would just reproduce the same forbidden-storm on a second account.
"""

import asyncio
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.crud import broadcast as broadcast_crud
from app.models.account import Account
from app.models.broadcast import Broadcast
from app.schemas.broadcast import BroadcastCreate
from app.services.telegram_actions import AccountNotAuthenticatedError, list_groups

logger = get_logger(__name__)

# Conservative starting default for how many groups one account should be asked
# to carry in a single broadcast before we split the work across other active
# accounts. Not a Telegram-documented limit — just a deliberately cautious
# number pending real usage data. Kept as a module constant so it's easy to
# tune later without touching call sites.
DISTRIBUTION_GROUP_THRESHOLD = 20


@dataclass
class DistributionPlan:
    # account_id -> group_ids assigned to that account
    assignments: dict[str, list[str]] = field(default_factory=dict)
    # True if the plan actually spreads work across >1 account (vs. a no-op
    # passthrough where everything still goes to the requesting account).
    distributed: bool = False


async def get_eligible_accounts(db: AsyncSession, tenant_id: str | None) -> list[Account]:
    """Active, non-suspended/banned accounts for a tenant.

    Mirrors the status checks Kiro's restriction-suspension logic relies on
    (app/crud/account.py suspend_account_for_restriction / app/services/
    delivery.py) — we filter independently here rather than depending on that
    code path, since this module should stay correct even if that logic
    changes shape later.
    """
    query = select(Account).where(Account.status == "active")
    if tenant_id is not None:
        query = query.where(Account.tenant_id == tenant_id)
    result = await db.execute(query)
    return list(result.scalars().all())


async def check_membership(
    accounts: list[Account], group_ids: list[str]
) -> dict[str, set[str]]:
    """For each account, which of the target group_ids it's actually a member of.

    Runs one live Telethon dialog listing per account, concurrently — this
    only happens once at broadcast-creation time for large group lists, not
    per message, so the latency (a couple seconds per account) is acceptable.
    Accounts that fail to list groups (session issues, etc.) are simply
    treated as having no memberships rather than failing the whole plan.
    """
    target = set(group_ids)

    async def _membership_for(account: Account) -> tuple[str, set[str]]:
        try:
            groups = await list_groups(account)
        except AccountNotAuthenticatedError:
            return account.id, set()
        except Exception as exc:
            logger.warning(
                "distribution_membership_check_failed", account_id=account.id, error=str(exc)
            )
            return account.id, set()
        member_ids = {g["id"] for g in groups}
        return account.id, member_ids & target

    results = await asyncio.gather(*(_membership_for(a) for a in accounts))
    return dict(results)


def plan_distribution(
    membership: dict[str, set[str]],
    group_ids: list[str],
    requesting_account_id: str,
) -> DistributionPlan:
    """Assign each group to the eligible member-account currently carrying the
    least load (round-robin by current count). Groups with no eligible member
    among the candidates fall back to the requesting account so nothing is
    silently dropped.
    """
    plan = DistributionPlan()
    load: dict[str, int] = {aid: 0 for aid in membership}

    for group_id in group_ids:
        eligible = [aid for aid, groups in membership.items() if group_id in groups]
        if not eligible:
            target_id = requesting_account_id
        else:
            target_id = min(eligible, key=lambda aid: load[aid])
            load[target_id] += 1
        plan.assignments.setdefault(target_id, []).append(group_id)

    plan.distributed = len(plan.assignments) > 1
    return plan


async def create_distributed_broadcast(
    db: AsyncSession,
    *,
    requesting_account: Account,
    target_ids: list[str],
    target_field: str,
    message: str,
    media_path: str | None,
    delivery_mode: str,
    delay_seconds: int | None,
    inline_buttons: list[dict] | None,
    reply_to_msg_id: int | None,
    scheduled_at,
    campaign_id: str | None,
) -> list[Broadcast]:
    """Split a list of chat targets across the tenant's eligible accounts and
    create one Broadcast row per account. Each row goes through the existing
    broadcast processor pipeline unmodified — distribution only changes how
    many chats each row is responsible for, not how a row is paced or retried.

    ``target_field`` says which Broadcast field ``target_ids`` came from and
    must be written back into on each child — this matters a lot:
    - "recipients": each id is a chat to message directly (the mode SendTab's
      group broadcast actually uses — a "recipient" here is a group's own
      chat id, one message per chat).
    - "group_ids": each id is a group whose *members* get resolved and
      messaged individually at dispatch time (see
      broadcast_processor.resolve_group_ids_to_recipients) — a completely
      different, much larger fan-out. Mixing these up would make a handful of
      groups explode into messaging every member of each one.

    Returns the created broadcasts with the requesting account's slice first
    (so callers that need a single "primary" broadcast for backward
    compatibility can just take index 0).
    """
    import uuid

    if target_field not in ("recipients", "group_ids"):
        raise ValueError(f"target_field must be 'recipients' or 'group_ids', got {target_field!r}")

    candidates = await get_eligible_accounts(db, requesting_account.tenant_id)
    # Always include the requesting account itself as a candidate, even if a
    # stricter status filter would exclude it for some reason — the caller
    # already validated it directly.
    if not any(a.id == requesting_account.id for a in candidates):
        candidates.append(requesting_account)

    membership = await check_membership(candidates, target_ids)
    plan = plan_distribution(membership, target_ids, requesting_account.id)

    batch_id = str(uuid.uuid4()) if plan.distributed else None

    created: list[Broadcast] = []
    for account_id, assigned_ids in plan.assignments.items():
        field_values = {
            "recipients": assigned_ids if target_field == "recipients" else [],
            "group_ids": assigned_ids if target_field == "group_ids" else None,
        }
        payload = BroadcastCreate(
            account_id=account_id,
            message=message,
            scheduled_at=scheduled_at,
            delivery_mode=delivery_mode,
            reply_to_msg_id=reply_to_msg_id,
            delay_seconds=delay_seconds,
            inline_buttons=inline_buttons,
            campaign_id=campaign_id,
            **field_values,
        )
        broadcast = await broadcast_crud.create_broadcast(
            db, payload, media_path, scheduled_at=scheduled_at
        )
        if batch_id is not None:
            broadcast.distribution_batch_id = batch_id
            await db.commit()
            await db.refresh(broadcast)
        created.append(broadcast)

    created.sort(key=lambda b: 0 if b.account_id == requesting_account.id else 1)
    return created
