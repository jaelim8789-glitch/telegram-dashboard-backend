"""Local development DB seed — creates dummy data for local-only testing.

Usage:
    cd telegram-dashboard-backend
    python scripts/seed_local.py

Requires a running local Postgres + backend service (or at least a reachable DB URL).
Idempotent: safe to re-run.
"""
import asyncio
import uuid
import random
from datetime import datetime, timedelta
from app.database import async_session_maker
from app.models import Account, User, PhoneVerification, Tenant
from app.models.team import TeamMember
from app.models.reply_macro import ReplyMacroRule, ReplyMacro
from app.models.message import MessageLog
from app.models.broadcast import Broadcast
from sqlalchemy import select


async def seed():
    async with async_session_maker() as db:
        # ── Tenant ──
        tenant_id = "demo-tenant-00001"
        existing = await db.get(Tenant, tenant_id)
        if not existing:
            db.add(Tenant(id=tenant_id, name="Demo Corp", settings={}))
            print("  + Tenant: Demo Corp")

        # ── Admin user ──
        admin_id = "demo-admin-00001"
        existing = await db.get(User, admin_id)
        if not existing:
            db.add(User(
                id=admin_id, tenant_id=tenant_id,
                username="admin", role="admin",
                full_name="Admin User", is_active=True,
            ))
            print("  + User: admin")

        # ── Regular users ──
        usernames = ["alice", "bob", "charlie", "diana"]
        for name in usernames:
            uid = f"demo-user-{name}"
            existing = await db.get(User, uid)
            if not existing:
                db.add(User(
                    id=uid, tenant_id=tenant_id,
                    username=name, role="user",
                    full_name=name.capitalize(), is_active=True,
                ))
                print(f"  + User: {name}")

        # ── Team members ──
        roles = ["manager", "operator", "viewer"]
        for i, name in enumerate(usernames[:3]):
            mid = f"demo-tm-{name}"
            existing = await db.get(TeamMember, mid)
            if not existing:
                db.add(TeamMember(
                    id=mid, tenant_id=tenant_id,
                    user_id=f"demo-user-{name}",
                    role=roles[i],
                ))
                print(f"  + TeamMember: {name} ({roles[i]})")

        # ── Telegram accounts ──
        phone_numbers = ["+821011111111", "+821022222222", "+821033333333"]
        for phone in phone_numbers:
            aid = f"demo-acc-{phone[-4:]}"
            existing = await db.get(Account, aid)
            if not existing:
                db.add(Account(
                    id=aid, tenant_id=tenant_id,
                    user_id=admin_id,
                    phone_number=phone,
                    status="active",
                    is_verified=True,
                    proxy_config={},
                ))
                print(f"  + Account: {phone}")

        # ── Phone verifications ──
        for phone in phone_numbers[:2]:
            pvid = f"demo-pv-{phone[-4:]}"
            existing = await db.get(PhoneVerification, pvid)
            if not existing:
                db.add(PhoneVerification(
                    id=pvid, tenant_id=tenant_id,
                    account_id=f"demo-acc-{phone[-4:]}",
                    phone_number=phone,
                    verification_code="123456",
                    status="verified",
                    verified_at=datetime.utcnow(),
                ))
                print(f"  + PhoneVerification: {phone}")

        # ── Reply macros (2 rules, 2 commands each) ──
        for rule_name, trigger in [("Auto Reply", "hello"), ("Support", "help")]:
            rule_id = f"demo-rule-{rule_name.lower().replace(' ', '-')}"
            existing = await db.get(ReplyMacroRule, rule_id)
            if not existing:
                db.add(ReplyMacroRule(
                    id=rule_id, tenant_id=tenant_id,
                    name=rule_name, trigger_keyword=trigger,
                    is_active=True, match_type="exact",
                ))
                print(f"  + ReplyMacroRule: {rule_name}")

            for i, (cmd, reply) in enumerate([
                ("hi", "Hello! How can I help you?"),
                ("info", "Here is the information you requested."),
            ]):
                macro_id = f"demo-macro-{rule_name.lower()}-{i}"
                existing = await db.get(ReplyMacro, macro_id)
                if not existing:
                    db.add(ReplyMacro(
                        id=macro_id, tenant_id=tenant_id,
                        rule_id=rule_id,
                        command=cmd, reply_text=reply,
                        is_active=True,
                    ))
                    print(f"  + ReplyMacro: {rule_name}/{cmd}")

        # ── Message logs ──
        account_ids = [f"demo-acc-{p[-4:]}" for p in phone_numbers]
        for i in range(20):
            mid = f"demo-msg-{i:04d}"
            existing = await db.get(MessageLog, mid)
            if not existing:
                ts = datetime.utcnow() - timedelta(hours=random.randint(1, 72))
                db.add(MessageLog(
                    id=mid, tenant_id=tenant_id,
                    account_id=random.choice(account_ids),
                    direction=random.choice(["incoming", "outgoing"]),
                    message_type="text",
                    content=f"Sample message #{i}",
                    status=random.choice(["sent", "delivered", "read"]),
                    sender=f"+8210{random.randint(10000000, 99999999)}",
                    created_at=ts,
                ))
        print("  + MessageLog: 20 entries")

        # ── Broadcasts ──
        for i in range(3):
            bid = f"demo-bcast-{i:04d}"
            existing = await db.get(Broadcast, bid)
            if not existing:
                db.add(Broadcast(
                    id=bid, tenant_id=tenant_id,
                    account_id=account_ids[0],
                    title=f"Demo campaign #{i}",
                    content=f"Broadcast message content {i}",
                    status=random.choice(["draft", "sent", "completed"]),
                    total_recipients=random.randint(50, 500),
                    created_at=datetime.utcnow() - timedelta(days=random.randint(0, 14)),
                ))
        print("  + Broadcast: 3 entries")

        await db.commit()
        print("\n✅ Seed complete. Run with a fresh DB to reset all data.")


if __name__ == "__main__":
    print("🌱 Seeding local demo data...")
    asyncio.run(seed())
