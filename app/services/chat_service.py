import re
from datetime import datetime, timezone

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Conversation, Message
from app.services.ai_core_service import call_deepseek


# ── 마스킹 ─────────────────────────────────────────────────────────────
_PHONE = re.compile(r"01[0-9][ -]?\d{3,4}[ -]?\d{4}")
_SSN = re.compile(r"\d{6}[ -]?\d{7}")
_EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


def mask_sensitive(text: str) -> str:
    text = _PHONE.sub("****", text)
    text = _SSN.sub("****", text)
    text = _EMAIL.sub("****", text)
    # IP addresses
    text = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "****", text)
    return text


# ── CRUD ────────────────────────────────────────────────────────────────

async def list_conversations(db: AsyncSession, tenant_id: str) -> list[Conversation]:
    result = await db.execute(
        select(Conversation)
        .where(Conversation.tenant_id == tenant_id)
        .order_by(Conversation.updated_at.desc())
    )
    return list(result.scalars().all())


async def create_conversation(db: AsyncSession, tenant_id: str, title: str = "새 대화") -> Conversation:
    c = Conversation(tenant_id=tenant_id, title=title)
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


async def delete_conversation(db: AsyncSession, conv_id: str) -> None:
    await db.execute(delete(Message).where(Message.conversation_id == conv_id))
    await db.execute(delete(Conversation).where(Conversation.id == conv_id))
    await db.commit()


async def get_messages(db: AsyncSession, conversation_id: str) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    return list(result.scalars().all())


async def add_message(db: AsyncSession, conversation_id: str, tenant_id: str, role: str, content: str) -> Message:
    masked = mask_sensitive(content)
    m = Message(conversation_id=conversation_id, tenant_id=tenant_id, role=role, content=masked)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


async def ask_ai(db: AsyncSession, conversation_id: str, tenant_id: str, question: str) -> str:
    """사용자 메시지 저장 → AI 응답 생성 → AI 응답 저장 → 응답 반환"""
    # 1. 사용자 메시지 저장
    await add_message(db, conversation_id, tenant_id, "user", question)

    # 2. 과거 메시지 불러오기
    msgs = await get_messages(db, conversation_id)
    history = [{"role": m.role, "content": m.content} for m in msgs]

    # 3. AI 호출
    answer = await call_deepseek(history, tenant_id=tenant_id)

    # 4. AI 응답 저장
    await add_message(db, conversation_id, tenant_id, "assistant", answer)

    # 5. 대화 제목 자동 생성 (첫 메시지 기준)
    if len(msgs) <= 1:
        conv = await db.get(Conversation, conversation_id)
        if conv:
            conv.title = question[:80] + ("..." if len(question) > 80 else "")
            await db.commit()

    return answer