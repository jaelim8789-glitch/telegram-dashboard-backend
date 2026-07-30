"""Local development DB seed — dummy accounts/broadcasts/macros for UI testing
without going through Telegram re-authentication every time.

Usage:
    cd telegram-dashboard-backend
    python scripts/seed_local.py

Requires a reachable DATABASE_URL (local Postgres). Idempotent: safe to re-run.

Seeded accounts have no session_data (no real Telethon session), so anything
that talks to Telegram live (chat list, message send) won't work against them —
this is for exercising account list / broadcast / reply-macro / health UI and
their non-Telegram-dependent API paths only.
"""
import asyncio
import random
from datetime import datetime, timedelta

from app.database import async_session_maker
from app.models.account import Account
from app.models.broadcast import Broadcast
from app.models.reply_macro import ReplyMacro

DEMO_PHONES = ["+8210" + str(random.randint(10000000, 99999999)) for _ in range(3)]
DEMO_ACCOUNT_IDS = [f"demo-acc-{i}" for i in range(len(DEMO_PHONES))]


async def seed():
    async with async_session_maker() as db:
        # ── Accounts ──────────────────────────────────────────────
        for aid, phone in zip(DEMO_ACCOUNT_IDS, DEMO_PHONES):
            existing = await db.get(Account, aid)
            if existing:
                continue
            db.add(Account(
                id=aid,
                phone=phone,
                name=f"Demo Account {aid[-1]}",
                status="active",
                today_sent=random.randint(0, 50),
                group_count=random.randint(0, 20),
            ))
            print(f"  + Account: {phone}")

        await db.flush()

        # ── Broadcasts ────────────────────────────────────────────
        for i in range(3):
            bid = f"demo-bcast-{i:04d}"
            if await db.get(Broadcast, bid):
                continue
            db.add(Broadcast(
                id=bid,
                account_id=DEMO_ACCOUNT_IDS[0],
                message=f"데모 발송 메시지 #{i}",
                recipients=[f"@demo_channel_{i}"],
                status=random.choice(["pending", "sent", "failed"]),
                created_at=datetime.utcnow() - timedelta(days=random.randint(0, 14)),
            ))
            print(f"  + Broadcast: demo-bcast-{i:04d}")

        # ── Reply macros ──────────────────────────────────────────
        for i, name in enumerate(["환영 메시지", "문의 자동응답"]):
            mid = f"demo-macro-{i:04d}"
            if await db.get(ReplyMacro, mid):
                continue
            db.add(ReplyMacro(
                id=mid,
                account_id=DEMO_ACCOUNT_IDS[0],
                name=name,
                is_active=True,
                target_chats="[]",
                message_content=f"{name} 본문입니다.",
            ))
            print(f"  + ReplyMacro: {name}")

        await db.commit()
        print("\nSeed complete. Accounts have no live Telegram session — "
              "chat/message features won't work against them, but account "
              "list, broadcast history, and reply macro UI will.")


if __name__ == "__main__":
    print("Seeding local demo data...")
    asyncio.run(seed())
