"""미니앱 전용 API 라우트 — DeepSeek 채팅, PixelOffice 상태"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from app.core.rate_limiter import check_rate_limit, get_client_ip, get_retry_after_seconds
from app.services.deepseek_service import chat_with_deepseek, parse_action

router = APIRouter(prefix="/api/miniapp", tags=["miniapp"])

_CHAT_LIMIT = dict(max_attempts=10, window_seconds=60)
_PIXEL_OFFICES_LIMIT = dict(max_attempts=30, window_seconds=60)

class ChatRequest(BaseModel):
    messages: list[dict]  # [{role: "user"|"agent", content: "..."}]

class ChatResponse(BaseModel):
    reply: str
    action: dict | None = None

@router.post("/chat", response_model=ChatResponse)
async def miniapp_chat(req: ChatRequest, request: Request):
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "miniapp_chat", **_CHAT_LIMIT):
        retry_after = get_retry_after_seconds(client_ip, "miniapp_chat", _CHAT_LIMIT["window_seconds"])
        raise HTTPException(
            status_code=429,
            detail=f"요청이 너무 많습니다. {retry_after}초 후 다시 시도해주세요.",
            headers={"Retry-After": str(retry_after)},
        )
    if not req.messages:
        raise HTTPException(400, "messages required")
    reply = await chat_with_deepseek(req.messages)
    action = parse_action(reply)
    clean_reply = reply
    import re
    clean_reply = re.sub(r'<ACTION>.*</ACTION>', '', reply, flags=re.DOTALL).strip()
    return ChatResponse(reply=clean_reply, action=action)

@router.get("/pixel-offices")
async def miniapp_pixel_offices(request: Request):
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "miniapp_pixel_offices", **_PIXEL_OFFICES_LIMIT):
        retry_after = get_retry_after_seconds(client_ip, "miniapp_pixel_offices", _PIXEL_OFFICES_LIMIT["window_seconds"])
        raise HTTPException(
            status_code=429,
            detail=f"요청이 너무 많습니다. {retry_after}초 후 다시 시도해주세요.",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        from app.database import async_session_maker
        from app.crud import account as account_crud
        from sqlalchemy import text
        async with async_session_maker() as db:
            result = await db.execute(text("SELECT id, name, status FROM pixel_offices LIMIT 3"))
            rows = result.fetchall()
            return [{"id": r[0], "name": r[1], "status": r[2]} for r in rows]
    except Exception:
        return []
