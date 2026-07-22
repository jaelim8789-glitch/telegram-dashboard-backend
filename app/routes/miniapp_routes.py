"""미니앱 전용 API 라우트 — DeepSeek 채팅, PixelOffice 상태"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.services.deepseek_service import chat_with_deepseek, parse_action

router = APIRouter(prefix="/api/miniapp", tags=["miniapp"])

class ChatRequest(BaseModel):
    messages: list[dict]  # [{role: "user"|"agent", content: "..."}]

class ChatResponse(BaseModel):
    reply: str
    action: dict | None = None

@router.post("/chat", response_model=ChatResponse)
async def miniapp_chat(req: ChatRequest):
    if not req.messages:
        raise HTTPException(400, "messages required")
    reply = await chat_with_deepseek(req.messages)
    action = parse_action(reply)
    clean_reply = reply
    import re
    clean_reply = re.sub(r'<ACTION>.*?</ACTION>', '', reply, flags=re.DOTALL).strip()
    return ChatResponse(reply=clean_reply, action=action)

@router.get("/pixel-offices")
async def miniapp_pixel_offices():
    try:
        from app.database import async_session_maker
        from app.crud import account as account_crud
        from sqlalchemy import text
        async with async_session_maker() as db:
            result = await db.execute(text("SELECT id, name, status FROM pixel_offices LIMIT 3"))
            rows = result.fetchall()
            return [{"id": r[0], "name": r[1], "status": r[2]} for r in rows]
    except:
        return []
