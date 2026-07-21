from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity
from app.core.logging import get_logger
from app.database import get_db
from app.services.chat_service import (
    list_conversations, create_conversation, update_conversation_title,
    delete_conversation, get_messages, ask_ai, ask_ai_stream,
)
from app.schemas.chat import (
    ConversationCreateResponse, ConversationRead, MessageAddResponse,
    MessageRead, StatusResponse,
)

router = APIRouter(prefix="/api/chat", tags=["chat"])
logger = get_logger(__name__)


@router.get("/conversations", response_model=list[ConversationRead])
async def get_conversations(
    search: str | None = Query(None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """대화 목록 조회 (search로 검색 가능)"""
    convs = await list_conversations(db, identity.tenant_id, search=search, limit=limit, offset=offset)
    return convs


@router.post("/conversations", response_model=ConversationCreateResponse)
async def new_conversation(
    body: dict | None = None,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """새 대화 생성 (선택적으로 title 지정 가능)"""
    title = body.get("title") if body else None
    c = await create_conversation(db, identity.tenant_id, title=title or "새 대화")
    return {"id": c.id, "title": c.title}


@router.patch("/conversations/{conv_id}", response_model=StatusResponse)
async def edit_conversation(
    conv_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """대화 제목 수정"""
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="제목을 입력하세요")
    await update_conversation_title(db, conv_id, identity.tenant_id, title)
    return {"status": "updated"}


@router.delete("/conversations/{conv_id}", response_model=StatusResponse)
async def remove_conversation(
    conv_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """대화 삭제"""
    await delete_conversation(db, conv_id, identity.tenant_id)
    return {"status": "deleted"}


@router.get("/conversations/{conv_id}/messages", response_model=list[MessageRead])
async def get_conversation_messages(
    conv_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """대화 메시지 조회"""
    msgs = await get_messages(db, conv_id)
    return msgs


@router.post("/conversations/{conv_id}/ask")
async def ask_question(
    conv_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """질문 → AI 응답 (비스트리밍)"""
    question = body.get("question", "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="질문을 입력하세요")
    answer = await ask_ai(db, conv_id, identity.tenant_id, question)
    return {"answer": answer}


@router.post("/conversations/{conv_id}/ask/stream")
async def ask_question_stream(
    conv_id: str,
    body: dict,
    identity: Identity = Depends(get_current_identity),
):
    """질문 → AI 응답 (SSE 스트리밍)"""
    question = body.get("question", "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="질문을 입력하세요")
    return StreamingResponse(
        ask_ai_stream(conv_id, identity.tenant_id, question),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )