"""Scale-validation DB seed — Epic 23.

Creates realistic volume for load/chaos tests:
  - 5 test accounts (no live session_data; real Telegram sessions are added
    by the operator for the 80%-real scenario)
  - 100 AI conversations + 10,000 messages (the DB volume the API/queue/scheduler
    actually touch)
  - 100 broadcast records (history the operations APIs read)
  - 100 auto-reply rules (the scheduler/auto-reply path)

Idempotent: safe to re-run. Targets a non-production DATABASE_URL only.

Usage:
    cd telegram-dashboard-backend
    python scripts/seed_scale.py [accounts=5] [conversations=100] [messages=10000]
"""
import asyncio
import random
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import async_session_maker
from app.models.account import Account
from app.models.auto_reply import AutoReplyRule
from app.models.broadcast import Broadcast
from app.models.chat import Conversation, Message


def _parse_args():
    conv = 100
    msgs = 10_000
    accts = 5
    for arg in sys.argv[1:]:
        if arg.startswith("accounts="):
            accts = int(arg.split("=")[1])
        elif arg.startswith("conversations="):
            conv = int(arg.split("=")[1])
        elif arg.startswith("messages="):
            msgs = int(arg.split("=")[1])
    return accts, conv, msgs


async def seed():
    n_accounts, n_conversations, n_messages = _parse_args()
    now = datetime.now(timezone.utc)

    async with async_session_maker() as db:
        # ── Accounts ──────────────────────────────────────────────
        account_ids = []
        for i in range(n_accounts):
            aid = f"scale-acc-{i:02d}"
            if await db.get(Account, aid) is None:
                db.add(Account(
                    id=aid,
                    phone=f"+8210{10000000 + i:08d}",
                    name=f"Scale Account {i:02d}",
                    status="active",
                    group_count=random.randint(20, 100),
                ))
                print(f"  + Account: scale-acc-{i:02d}")
            account_ids.append(aid)
        await db.flush()

        # ── Broadcast history ─────────────────────────────────────
        for i in range(100):
            bid = f"scale-bcast-{i:04d}"
            if await db.get(Broadcast, bid) is None:
                db.add(Broadcast(
                    id=bid,
                    account_id=account_ids[i % n_accounts],
                    message=f"스케일 테스트 발송 #{i}",
                    recipients=[f"@scale_channel_{j}" for j in range(random.randint(1, 20))],
                    status=random.choice(["pending", "sent", "failed"]),
                    created_at=now - timedelta(minutes=random.randint(0, 600)),
                ))
        print(f"  + Broadcast history: 100")

        # ── Auto-reply rules ──────────────────────────────────────
        for i in range(100):
            rid = f"scale-ar-{i:04d}"
            if await db.get(AutoReplyRule, rid) is None:
                db.add(AutoReplyRule(
                    id=rid,
                    account_id=account_ids[i % n_accounts],
                    name=f"Scale AutoReply {i}",
                    is_active=True,
                    match_type="keyword",
                    match_value=f"키워드{i}",
                    reply_content=f"자동응답 #{i} 본문입니다.",
                ))
        print(f"  + Auto-reply rules: 100")

        # ── AI conversations + messages ───────────────────────────
        # Ensure a parent conversation exists for each message.
        conv_ids = []
        for i in range(n_conversations):
            cid = f"scale-conv-{i:04d}"
            if await db.get(Conversation, cid) is None:
                db.add(Conversation(
                    id=cid,
                    tenant_id=account_ids[0],
                    title=f"Scale Conversation {i}",
                ))
            conv_ids.append(cid)
        await db.flush()

        existing_msgs = (await db.execute(select(Message.id))).scalars().all()
        existing_count = len([m for m in existing_msgs if m.startswith("scale-msg-")])
        need = n_messages - existing_count
        if need > 0:
            batch = []
            for k in range(need):
                cid = conv_ids[k % n_conversations]
                batch.append(Message(
                    id=f"scale-msg-{existing_count + k:06d}",
                    conversation_id=cid,
                    tenant_id=account_ids[0],
                    role="assistant" if k % 2 else "user",
                    content=f"스케일 테스트 메시지 {existing_count + k} — " + "가나다라마바사아자차카타파하 " * random.randint(1, 20),
                    created_at=now - timedelta(seconds=random.randint(0, 86_400)),
                ))
                if len(batch) >= 1000:
                    db.add_all(batch)
                    await db.flush()
                    batch = []
            if batch:
                db.add_all(batch)
            print(f"  + Messages: {need} added")

        await db.commit()
        print(f"\nSeed complete: accounts={n_accounts} conversations={n_conversations} messages={n_messages} + 100 broadcasts + 100 auto-reply rules")


if __name__ == "__main__":
    print("Seeding scale-validation data...")
    asyncio.run(seed())
