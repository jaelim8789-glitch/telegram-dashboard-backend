import json
import re
from datetime import datetime, timezone

from sqlalchemy import select, delete, or_, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Conversation, Message
from app.services.ai_core_service import call_deepseek

# ── 시스템 프롬프트 ─────────────────────────────────────────────────────
SYSTEM_PROMPT = """당신은 TeleMon의 AI 운영 비서입니다. TeleMon은 Telegram 계정 운영을 자동화하는 플랫폼입니다.
당신의 역할:
- 사용자의 Telegram 계정 상태 분석 및 모니터링 지원
- 메시지 발송, 자동 답장, 그룹 관리 등 운영 작업 지원
- 운영 리포트 및 인사이트 제공
- 이상 징후 감지 및 대응 방안 제안
- 반복 업무 자동화 방안 제안

항상 친절하고 실용적인 조언을 제공하며, 사용자가 TeleMon의 기능을 최대한 활용할 수 있도록 도와주세요."""

# ── 마스킹 ─────────────────────────────────────────────────────────────
_PHONE = re.compile(r"01[0-9][ -]?\d{3,4}[ -]?\d{4}")
_SSN = re.compile(r"\d{6}[ -]?\d{7}")
_EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

def mask_sensitive(text: str) -> str:
    text = _PHONE.sub("****", text)
    text = _SSN.sub("****", text)
    text = _EMAIL.sub("****", text)
    text = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "****", text)
    return text

# ── CRUD ────────────────────────────────────────────────────────────────

async def list_conversations(db: AsyncSession, tenant_id: str, search: str | None = None) -> list[Conversation]:
    query = select(Conversation).where(Conversation.tenant_id == tenant_id)
    if search:
        query = query.where(Conversation.title.ilike(f"%{search}%"))
    query = query.order_by(Conversation.updated_at.desc())
    result = await db.execute(query)
    return list(result.scalars().all())

async def create_conversation(db: AsyncSession, tenant_id: str, title: str = "새 대화") -> Conversation:
    c = Conversation(tenant_id=tenant_id, title=title)
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c

async def update_conversation_title(db: AsyncSession, conv_id: str, title: str) -> None:
    await db.execute(update(Conversation).where(Conversation.id == conv_id).values(title=title))
    await db.commit()

async def delete_conversation(db: AsyncSession, conv_id: str) -> None:
    await db.execute(delete(Message).where(Message.conversation_id == conv_id))
    await db.execute(delete(Conversation).where(Conversation.id == conv_id))
    await db.commit()

async def get_messages(db: AsyncSession, conversation_id: str) -> list[Message]:
    result = await db.execute(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)
    )
    return list(result.scalars().all())

async def add_message(db: AsyncSession, conversation_id: str, tenant_id: str, role: str, content: str) -> Message:
    masked = mask_sensitive(content) if role == "user" else content
    m = Message(conversation_id=conversation_id, tenant_id=tenant_id, role=role, content=masked)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m

async def ask_ai(db: AsyncSession, conversation_id: str, tenant_id: str, question: str) -> str:
    # 1. 사용자 메시지 저장
    await add_message(db, conversation_id, tenant_id, "user", question)

    # 2. 과거 메시지 불러오기
    msgs = await get_messages(db, conversation_id)
    # 시스템 프롬프트 + 히스토리 구성
    history = [{"role": "system", "content": SYSTEM_PROMPT}]
    history.extend({"role": m.role, "content": m.content} for m in msgs)

    # 3. AI 호출
    answer, _ = await call_deepseek(history, max_tokens=2000)

    # 4. AI 응답 저장
    if answer:
        await add_message(db, conversation_id, tenant_id, "assistant", answer)

        # 5. 대화 제목 자동 생성 (첫 대화)
        if len(msgs) <= 1:
            title = question[:60] + ("..." if len(question) > 60 else "")
            await update_conversation_title(db, conversation_id, title)

        return answer
    return "죄송합니다. 응답을 생성하는 중에 오류가 발생했습니다."

async def ask_ai_stream(conversation_id: str, tenant_id: str, question: str):
    """SSE 스트리밍 버전. async generator가 chunks를 yield."""
    from app.database import async_session_maker
    from app.services.ai_core_service import _call_deepseek_stream

    # DB 저장은 별도 세션
    async with async_session_maker() as db:
        await add_message(db, conversation_id, tenant_id, "user", question)
        msgs = await get_messages(db, conversation_id)

    history = [{"role": "system", "content": SYSTEM_PROMPT}]
    history.extend({"role": m.role, "content": m.content} for m in msgs)

    full = ""
    async for chunk in _call_deepseek_stream(history, max_tokens=2000):
        if chunk:
            full += chunk
            yield json.dumps({"token": chunk}) + "\n"

    # 전체 응답 DB 저장
    async with async_session_maker() as db:
        await add_message(db, conversation_id, tenant_id, "assistant", full)
        if len(msgs) <= 1:
            title = question[:60] + ("..." if len(question) > 60 else "")
            await update_conversation_title(db, conversation_id, title)

    yield json.dumps({"done": True, "full": full}) + "\n"