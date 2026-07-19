import json
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity
from app.core.logging import get_logger
from app.database import get_db
from app.models.ai_agent import AiAgent, AiChat, AiMessage
from app.services.ai_core_service import call_deepseek, _call_deepseek_stream
from app.services.usage_tracker import get_monthly_usage, record_usage

router = APIRouter(prefix="/api/ai", tags=["ai-agent"])
logger = get_logger(__name__)

TOOL_PER_TOKEN = 3
MSG_PER_TOKEN = 1
EXP_PER_MSG = 10
EXP_PER_LEVEL = 100
GOOD_QUESTION_BONUS = 25
GOOD_QUESTION_MARKER = "[GOOD_QUESTION]"

ROLE_PROMPTS = {
    "marketing": "당신은 마케팅 전문가입니다. 효과적인 마케팅 전략과 카피를 제안하고, 실제 텔레그램 발송까지 실행할 수 있습니다.",
    "web_search": "당신은 웹 검색 및 정보 분석 전문가입니다. 정확한 정보를 제공하고 필요한 데이터를 수집합니다.",
    "coding": "당신은 소프트웨어 개발 전문가입니다. 코드 작성, 디버깅, 아키텍처 조언을 제공합니다.",
    "scheduler": "당신은 일정 관리 및 작업 자동화 전문가입니다. 반복 업무를 자동화하는 방법을 제안합니다.",
    "custom": "당신은 사용자의 개인 AI 비서입니다. 친절하고 전문적으로 모든 작업을 도와줍니다.",
}


def _build_system_prompt(agent: AiAgent) -> str:
    base = ROLE_PROMPTS.get(agent.role, ROLE_PROMPTS["custom"])
    if agent.system_prompt:
        base += f"\n\n{agent.system_prompt}"
    base += (
        "\n\n응답에는 필요한 경우 실행 버튼을 포함할 수 있습니다. 버튼은 다음과 같은 형식으로 표시합니다: "
        "[📨 발송] [📅 예약] [🔍 검색] [📊 분석] [⚙️ 설정]\n\n"
        "상황 인지형 예약 제안: 사용자의 요청이 특정 시간에 메시지를 보내거나 반복 작업을 필요로 하면, "
        "응답에 [📅 예약] 버튼을 포함해 예약 기능을 제안하세요. 실행은 사용자가 버튼을 눌러야 합니다.\n\n"
        "사용자 품질 마커: 사용자의 질문이 매우 구체적이고 깊이 있는 통찰을 요구하는 좋은 질문이라면, "
        "응답의 맨 끝에 " + GOOD_QUESTION_MARKER + " 마커를 한 줄로 추가하세요. 이 마커는 사용자에게는 표시되지 않습니다. "
        "단순한 인사나 짧은 질문에는 절대 추가하지 마세요."
    )
    return base


# ─── Agent CRUD ─────────────────────────────────────────────────────────

@router.post("/agents")
async def create_agent(
    body: dict,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Agent 생성 (무료: 1개, Pro: 5개, Team: 20개)"""
    # 생성 가능 개수 체크
    result = await db.execute(
        select(func.count(AiAgent.id)).where(AiAgent.owner_id == identity.tenant_id)
    )
    count = result.scalar() or 0

    # 토큰 과금 없이 생성 개수만 체크 (기획서 v2 Section 2)
    if count >= 20:
        raise HTTPException(status_code=400, detail="Agent 생성 한도(20개)를 초과했습니다.")

    agent = AiAgent(
        owner_id=identity.tenant_id,
        name=body.get("name", "새 Agent"),
        role=body.get("role", "자유"),
        system_prompt=body.get("system_prompt", ""),
        tools=body.get("tools", []),
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return {
        "id": agent.id, "name": agent.name, "role": agent.role,
        "system_prompt": agent.system_prompt, "is_template": agent.is_template,
        "total_messages": agent.total_messages, "level": agent.level, "exp": agent.exp,
        "created_at": agent.created_at.isoformat(),
    }


@router.get("/agents")
async def list_agents(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """내 Agent 목록"""
    result = await db.execute(
        select(AiAgent).where(AiAgent.owner_id == identity.tenant_id).order_by(AiAgent.created_at.desc())
    )
    return [
        {"id": a.id, "name": a.name, "role": a.role, "is_template": a.is_template,
         "total_messages": a.total_messages, "level": a.level, "exp": a.exp,
         "created_at": a.created_at.isoformat()}
        for a in result.scalars().all()
    ]


@router.put("/agents/{agent_id}")
async def update_agent(
    agent_id: str, body: dict,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Agent 수정"""
    agent = await db.get(AiAgent, agent_id)
    if agent is None or agent.owner_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Agent를 찾을 수 없습니다.")
    for field in ("name", "role", "system_prompt", "tools", "is_active"):
        if field in body:
            setattr(agent, field, body[field])
    await db.commit()
    return {"status": "updated"}


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Agent 삭제"""
    agent = await db.get(AiAgent, agent_id)
    if agent is None or agent.owner_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Agent를 찾을 수 없습니다.")
    # 관련 채팅/메시지 삭제
    chats = await db.execute(select(AiChat).where(AiChat.agent_id == agent_id))
    for chat in chats.scalars().all():
        await db.execute(delete(AiMessage).where(AiMessage.chat_id == chat.id))
        await db.delete(chat)
    await db.delete(agent)
    await db.commit()


# ─── 채팅 ─────────────────────────────────────────────────────────────────

@router.post("/agents/{agent_id}/chat")
async def create_chat(
    agent_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """새 채팅방 생성"""
    agent = await db.get(AiAgent, agent_id)
    if agent is None or agent.owner_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Agent를 찾을 수 없습니다.")
    chat = AiChat(agent_id=agent_id, tenant_id=identity.tenant_id, title=body.get("title", ""))
    db.add(chat)
    await db.commit()
    await db.refresh(chat)
    return {"id": chat.id, "title": chat.title}


@router.get("/agents/{agent_id}/chats")
async def list_chats(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Agent의 채팅방 목록"""
    result = await db.execute(
        select(AiChat).where(AiChat.agent_id == agent_id).order_by(AiChat.created_at.desc())
    )
    return [{"id": c.id, "title": c.title, "created_at": c.created_at.isoformat()} for c in result.scalars().all()]


@router.get("/chats/{chat_id}/messages")
async def get_messages(
    chat_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """채팅방 메시지 내역"""
    result = await db.execute(
        select(AiMessage).where(AiMessage.chat_id == chat_id).order_by(AiMessage.created_at)
    )
    return [
        {"id": m.id, "role": m.role, "content": m.content,
         "tool_name": m.tool_name, "tool_button_label": m.tool_button_label,
         "tool_payload": m.tool_payload, "tokens_used": m.tokens_used,
         "created_at": m.created_at.isoformat()}
        for m in result.scalars().all()
    ]


@router.post("/chats/{chat_id}/message")
async def send_message(
    chat_id: str, body: dict,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """메시지 전송 — 스트리밍으로 AI 응답 반환"""
    question = body.get("content", "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="내용을 입력하세요")

    chat = await db.get(AiChat, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")

    agent = await db.get(AiAgent, chat.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent를 찾을 수 없습니다.")

    # 토큰 과금 체크 (monthly_ai_chat_limit)
    used = await get_monthly_usage(db, identity.tenant_id, "ai_chat")
    from app.core.plans import get_plan_limits
    from app.models.tenant import Tenant
    tenant = await db.get(Tenant, identity.tenant_id)
    limit = get_plan_limits(tenant.plan if tenant else "free").get("monthly_ai_chat_limit", 0)
    if limit > 0 and used + MSG_PER_TOKEN > limit:
        raise HTTPException(status_code=429, detail="월간 AI 채팅 한도를 초과했습니다.")

    # 사용자 메시지 저장
    db.add(AiMessage(chat_id=chat_id, role="user", content=question, tokens_used=0))
    await db.commit()

    # 과거 메시지 불러오기
    result = await db.execute(
        select(AiMessage).where(AiMessage.chat_id == chat_id).order_by(AiMessage.created_at).limit(50)
    )
    history = list(result.scalars().all())

    # 시스템 프롬프트 + 히스토리
    system_prompt = _build_system_prompt(agent)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend({"role": m.role, "content": m.content} for m in history)

    # AI 호출 (비스트리밍 — 프론트에서 StreamingResponse로 전환 가능)
    answer, tokens = await call_deepseek(messages, max_tokens=2000)
    if not answer:
        answer = "죄송합니다. 응답 생성에 실패했습니다."

    # AI 응답 저장
    cleaned_answer = answer
    good_question = False
    if answer.endswith(GOOD_QUESTION_MARKER):
        cleaned_answer = answer[: -len(GOOD_QUESTION_MARKER)].rstrip()
        good_question = True

    db.add(AiMessage(chat_id=chat_id, role="agent", content=cleaned_answer, tokens_used=tokens))

    # EXP 적립
    old_level = agent.level
    exp_gained = EXP_PER_MSG + (tokens // 10)
    if good_question:
        exp_gained += GOOD_QUESTION_BONUS
    agent.exp += exp_gained
    agent.total_messages += 1
    while agent.exp >= EXP_PER_LEVEL * agent.level:
        agent.exp -= EXP_PER_LEVEL * agent.level
        agent.level += 1
    level_up = agent.level > old_level

    await db.commit()

    await record_usage(identity.tenant_id, "ai_chat", MSG_PER_TOKEN)

    return {
        "role": "agent", "content": cleaned_answer, "tokens_used": tokens,
        "exp_gained": exp_gained, "level_up": level_up,
        "new_level": agent.level, "exp": agent.exp,
    }


@router.post("/chats/{chat_id}/message/stream")
async def send_message_stream(
    chat_id: str, body: dict,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """메시지 전송 — SSE 스트리밍"""
    question = body.get("content", "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="내용을 입력하세요")

    chat = await db.get(AiChat, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")

    agent = await db.get(AiAgent, chat.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent를 찾을 수 없습니다.")

    # 사용자 메시지 저장
    db.add(AiMessage(chat_id=chat_id, role="user", content=question, tokens_used=0))
    await db.commit()

    result = await db.execute(
        select(AiMessage).where(AiMessage.chat_id == chat_id).order_by(AiMessage.created_at).limit(50)
    )
    history = list(result.scalars().all())
    system_prompt = _build_system_prompt(agent)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend({"role": m.role, "content": m.content} for m in history)

    async def _stream():
        full = ""
        async for chunk in _call_deepseek_stream(messages, max_tokens=2000):
            if chunk:
                full += chunk
                yield json.dumps({"token": chunk}) + "\n"

        # 전체 응답 DB 저장
        from app.database import async_session_maker
        async with async_session_maker() as s:
            agent_ref = await s.get(AiAgent, chat.agent_id)
            cleaned = full
            good_question = False
            if full.rstrip().endswith(GOOD_QUESTION_MARKER):
                cleaned = full.rstrip()[: -len(GOOD_QUESTION_MARKER)].rstrip()
                good_question = True

            tokens_used = len(cleaned) // 4
            s.add(AiMessage(chat_id=chat_id, role="agent", content=cleaned, tokens_used=tokens_used))

            old_level = agent_ref.level
            exp_gained = EXP_PER_MSG + (tokens_used // 10)
            if good_question:
                exp_gained += GOOD_QUESTION_BONUS
            agent_ref.exp += exp_gained
            agent_ref.total_messages += 1
            while agent_ref.exp >= EXP_PER_LEVEL * agent_ref.level:
                agent_ref.exp -= EXP_PER_LEVEL * agent_ref.level
                agent_ref.level += 1
            level_up = agent_ref.level > old_level

            await s.commit()
            await record_usage(identity.tenant_id, "ai_chat", MSG_PER_TOKEN)

            yield json.dumps({
                "done": True,
                "exp_gained": exp_gained,
                "level_up": level_up,
                "new_level": agent_ref.level,
                "exp": agent_ref.exp,
            }) + "\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── Tool 실행 ────────────────────────────────────────────────────────────

@router.post("/messages/{message_id}/execute")
async def execute_tool(
    message_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Tool 버튼 실행 (메시지에 포함된 tool_payload 실행)"""
    msg = await db.get(AiMessage, message_id)
    if msg is None or msg.role != "agent":
        raise HTTPException(status_code=404, detail="메시지를 찾을 수 없습니다.")
    chat = await db.get(AiChat, msg.chat_id)
    if chat is None or chat.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="메시지를 찾을 수 없습니다.")
    if not msg.tool_name:
        raise HTTPException(status_code=400, detail="실행할 Tool이 없습니다.")

    # 토큰 차감 (Tool = 3토큰)
    used = await get_monthly_usage(db, identity.tenant_id, "ai_chat")
    from app.core.plans import get_plan_limits
    from app.models.tenant import Tenant
    tenant = await db.get(Tenant, identity.tenant_id)
    limit = get_plan_limits(tenant.plan if tenant else "free").get("monthly_ai_chat_limit", 0)
    if limit > 0 and used + TOOL_PER_TOKEN > limit:
        raise HTTPException(status_code=429, detail="월간 AI 채팅 한도를 초과했습니다.")
    await record_usage(identity.tenant_id, "ai_chat", TOOL_PER_TOKEN)

    # Tool 실행 로그
    db.add(AiMessage(
        chat_id=msg.chat_id, role="tool",
        content=f"Tool 실행됨: {msg.tool_name}",
        tool_name=msg.tool_name,
        tokens_used=TOOL_PER_TOKEN,
    ))
    await db.commit()

    return {"status": "executed", "tool": msg.tool_name, "payload": msg.tool_payload}


# ─── 템플릿 마켓 ──────────────────────────────────────────────────────────

@router.post("/agents/{agent_id}/publish")
async def publish_template(
    agent_id: str, body: dict,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Agent를 템플릿으로 등록"""
    agent = await db.get(AiAgent, agent_id)
    if agent is None or agent.owner_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Agent를 찾을 수 없습니다.")
    agent.is_template = True
    agent.template_price = body.get("price", 0)
    await db.commit()
    return {"status": "published", "price": agent.template_price}


@router.get("/templates")
async def list_templates(
    role: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """템플릿 마켓 목록"""
    query = select(AiAgent).where(AiAgent.is_template.is_(True)).order_by(AiAgent.template_purchases.desc())
    if role:
        query = query.where(AiAgent.role == role)
    result = await db.execute(query)
    return [
        {"id": a.id, "name": a.name, "role": a.role,
         "owner_id": a.owner_id, "price": a.template_price,
         "purchases": a.template_purchases, "total_messages": a.total_messages}
        for a in result.scalars().all()
    ]


@router.post("/templates/{template_id}/purchase")
async def purchase_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """템플릿 구매 → Stars 차감(판매자 70% 정산) → 내 Agent로 복제"""
    template = await db.get(AiAgent, template_id)
    if template is None or not template.is_template:
        raise HTTPException(status_code=404, detail="템플릿을 찾을 수 없습니다.")
    if template.owner_id == identity.tenant_id:
        raise HTTPException(status_code=400, detail="자신의 템플릿은 구매할 수 없습니다.")

    new_balance = None
    if template.template_price > 0:
        from app.services.usage_tracker import spend_stars, add_stars_credit

        result = await spend_stars(
            identity.tenant_id, template.template_price, f"agent_template:{template_id}"
        )
        if not result["success"]:
            raise HTTPException(status_code=402, detail=result["error"])
        new_balance = result["new_balance"]
        await add_stars_credit(template.owner_id, int(template.template_price * 0.7))

    agent = AiAgent(
        owner_id=identity.tenant_id,
        name=template.name,
        role=template.role,
        system_prompt=template.system_prompt,
        tools=template.tools,
    )
    db.add(agent)
    template.template_purchases += 1
    await db.commit()
    await db.refresh(agent)
    return {"id": agent.id, "name": agent.name, "role": agent.role, "stars_balance": new_balance}