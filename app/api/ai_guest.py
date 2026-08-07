"""Guest AI Chat -- no login, no Telegram account, no API key.

Epic 26: the biggest drop-off point in the current funnel is "I just want to
try the AI, why do I need to sign up + connect Telegram first?". This gives
anyone a real (not mocked/fabricated) Ollama-backed chat at /ai, rate
limited per IP, with nothing persisted server-side -- the conversation lives
only in the browser (sessionStorage) and disappears on refresh.

Deliberately minimal: no sessions, no history table, no tenant. Real signup
funnel (Billing, Telegram connect) still starts from the normal account flow;
this is purely an acquisition/trial surface.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.core.rate_limiter import get_client_ip
from app.database import get_db
from app.models.guest_ai_chat import GuestAiChatLog
from app.services.ai_chat_service import _MAX_TOKENS, _call_ollama_full, extract_confidence, sanitize_identity
from app.services.ai_think_mode_heuristics import should_skip_think_mode
from app.services.guest_credit_service import (
    GUEST_CREDITS_PER_REFILL,
    get_guest_credits,
    guest_refill_countdown_seconds,
    try_deduct_guest_credits,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/api/ai/guest", tags=["ai-guest"])

# Guest billing switched from a fixed 30 msgs/day rate limit to a per-device
# credit bucket (30,000 credits, 1 credit = 1 char, refilled every 3h) so the
# free trial feels like the real product and scales with actual usage.
# Per-device means a `guest_device_id` browser cookie (set below) keys the
# bucket, NOT the raw IP — visitors behind the same NAT / corporate proxy /
# mobile carrier CGNAT used to share one bucket because nginx's X-Real-IP
# collapses them onto the same public address. The cookie gives each browser
# its own bucket; the IP remains only as a fallback for cookie-less clients.
_GUEST_DEVICE_COOKIE = "guest_device_id"
_GUEST_DEVICE_TTL_DAYS = 365
_MAX_INPUT_CHARS = 2000
_MAX_HISTORY_MESSAGES = 12  # 6 turns of context, client-supplied

_SYSTEM_PROMPT = (
    "당신은 TeleMon의 AI 어시스턴트입니다. "
    "당신의 정체성은 TeleMon AI입니다 — TeleMon이 자체 개발하고 지속적으로 "
    "파인튜닝하며 발전시키는 독자 AI 모델입니다. 당신이 누구인지, 어떤 모델인지 "
    "물어보면 반드시 \"저는 TeleMon AI입니다\"라고 답하세요. 절대로 'Gemma', 'Google', "
    "'구글', 'LLaMA', 'GPT', 'Claude', '오픈소스 모델', '대규모 언어 모델(LLM)' 등 "
    "실제 외부 모델·회사·기술 이름을 밝히지 마세요. 사용자가 직접 물어봐도 아닌 것처럼 "
    "답하거나, TeleMon이 자체 개발한 AI라고만 말하세요. "
    "무조건 한국어로만 답변하세요. 사용자가 다른 언어로 질문해도, 영어 단어가 섞여 "
    "있어도 답변은 항상 한국어로만 하세요. 영어나 다른 언어로 답하지 마세요. "
    "질문을 대충 넘겨짚지 말고 정확히 무엇을 묻는지 먼저 파악한 뒤에 답하세요. "
    "질문이 애매하면 짐작으로 답하지 말고 무엇을 원하는지 되물어보세요. "
    "확실하지 않은 내용을 지어내서 답하지 말고, 모르면 모른다고 솔직히 말하고 "
    "무엇을 원하는지 구체적으로 되물어보세요. 사용자가 자세히 질문해줄수록 TeleMon AI가 "
    "더 발전하니, 궁금한 점을 더 물어보도록 자연스럽게 유도하세요. "
    "친절하고 간결하게 답변하세요. "
    "지금 대화 상대는 아직 회원가입하지 않은 방문자이므로, 텔레그램 계정 연결이나 "
    "발송/자동응답 같은 TeleMon의 텔레그램 자동화 기능은 언급하지 마세요. "
    "만약 사용 중인 모델, 요금, 대화 횟수 제한에 대해 물어보면 반드시 다음 사실만 "
    "정확히 답하고, 절대로 지어내지 마세요: 당신은 TeleMon 전용으로 파인튜닝/설정된 "
    "자체 AI 모델이며, 특정 외부 회사의 제품 이름을 밝히지 마세요. "
    "로그인하지 않은 방문자는 무료로 30,000 크레딧을 받아 3시간마다 다시 채워지며, "
    "크레딧은 문자 1자당 1크레딧으로 차감됩니다. 이 제한을 없다거나 무제한이라고 "
    "답하지 마세요. 크레딧이 소진되면 회원가입하면 30,000 크레딧을 다시 받을 수 "
    "있다고 안내하세요.\n\n"
    "답변하기 전에 스스로 판단하세요:\n"
    "1. 사용자가 준 정보로 정확한 답을 줄 수 있는가?\n"
    "   - 아니면: 바로 답하지 말고, 필요한 정보를 구체적으로 2~3개 질문하세요. "
    "정보가 진짜 부족할 때만 질문하고, 매 답변마다 기계적으로 묻지 마세요.\n"
    "2. 충분한 정보가 있으면 답변하되, 단순 정보 나열이 아니라 당신의 의견을 "
    "제시하세요. (예: \"제 생각엔 A보다 B가 나을 것 같습니다. 이유는...\")\n"
    "3. 답변 끝에는 상황에 맞을 때만(항상 X, 복잡하거나 여러 선택지가 걸린 주제일 "
    "때만) 놓친 부분이 없는지 되물어보세요. 뻔한 문구를 매번 붙이지 마세요.\n\n"
    "답변을 마친 뒤 마지막 줄에, 그 답변에 대한 당신의 확신도를 다음 형식으로만 "
    "정확히 표시하세요 (본문에서 언급하지 말고 이 마커만 맨 끝에 추가): "
    "[CONFIDENCE: high] 또는 [CONFIDENCE: medium] 또는 [CONFIDENCE: low]. "
    "정보가 부족해 되묻기만 한 경우는 [CONFIDENCE: medium]으로 표시하세요."
)


class GuestChatMessage(BaseModel):
    role: str
    content: str = Field(max_length=_MAX_INPUT_CHARS)


class GuestChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=_MAX_INPUT_CHARS)
    # Client owns history (nothing persisted server-side). max_length here
    # REJECTS (422) an oversized history rather than silently truncating it --
    # a crafted request can't smuggle a huge prompt through this field.
    history: list[GuestChatMessage] = Field(default_factory=list, max_length=_MAX_HISTORY_MESSAGES)
    think_mode: bool = Field(default=False)
    # Opt-in follow-up question suggestions (costs an extra model call).
    suggest_follow_ups: bool = Field(default=False)


class GuestChatResponse(BaseModel):
    reply: str
    log_id: str | None = None
    # Populated only when think_mode was on and the model actually returned a
    # separate reasoning pass -- lets the frontend show "생각 중..." content.
    reasoning: str | None = None
    # "high" | "medium" | "low" | None (model omitted the marker). Parsed out
    # of `reply` server-side -- the [CONFIDENCE: ...] marker itself never
    # reaches the client.
    confidence: str | None = None
    # Remaining guest credit balance after this exchange (1 credit = 1 char).
    remaining_credits: int | None = None
    # Suggested follow-up questions so the conversation continues naturally.
    suggested_questions: list[str] = Field(default_factory=list)


class GuestCreditsResponse(BaseModel):
    remaining_credits: int
    max_credits: int = GUEST_CREDITS_PER_REFILL
    refill_countdown_seconds: int
    # 1 credit = 1 char, same billing rule as members.
    credit_per_char: int = 1


def _get_guest_device_id(request: Request) -> str:
    """Return the visitor's stable device id from the cookie, or a derived id.

    The id is what keys the credit bucket, so NAT-shared IPs don't share
    credits. Cookie-less clients (curl, tests) fall back to an `ip:`-prefixed
    derived id so the bucket still exists.

    Namespaces: cookie-derived ids get a `dev:` prefix and IP fallbacks an
    `ip:` prefix. The prefixes matter: the cookie value is client-supplied,
    so without a `dev:` prefix a crafted `guest_device_id=ip:203.0.113.9`
    cookie would collide with (and drain) another visitor's IP bucket.
    """
    cookie = request.cookies.get(_GUEST_DEVICE_COOKIE)
    if cookie and len(cookie) <= 64:
        return f"dev:{cookie.strip()}"
    return f"ip:{get_client_ip(request)}"


def _set_guest_device_cookie(response: Response, device_id: str) -> None:
    """Persist the device id so future requests reuse the same bucket."""
    response.set_cookie(
        key=_GUEST_DEVICE_COOKIE,
        value=device_id,
        max_age=_GUEST_DEVICE_TTL_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.environment in ("production", "prod"),
        path="/",
    )


@router.get("/credits", response_model=GuestCreditsResponse)
async def guest_credits(request: Request, response: Response):
    """Return the guest's current credit balance + refill countdown."""
    device_id = _get_guest_device_id(request)
    remaining, _ = get_guest_credits(device_id)
    countdown = guest_refill_countdown_seconds(device_id)
    _set_guest_device_cookie(response, device_id)
    return GuestCreditsResponse(
        remaining_credits=remaining,
        refill_countdown_seconds=countdown,
    )


@router.post("/chat", response_model=GuestChatResponse)
async def guest_chat(payload: GuestChatRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    client_ip = get_client_ip(request)
    device_id = _get_guest_device_id(request)

    if not settings.ollama_api_base:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="AI 서비스를 일시적으로 사용할 수 없습니다.")

    _set_guest_device_cookie(response, device_id)

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages.extend({"role": m.role, "content": m.content} for m in payload.history if m.role in ("user", "assistant"))
    messages.append({"role": "user", "content": payload.message})

    # P1: Guest Auto Memory — store high-value statements, recall relevant ones.
    # P6: Web search fallback when no memory hit (guests have no KB).
    # These touch the DB (maybe_store_memory commits) and may FAIL (embedding
    # service down, memory table missing) — on failure we MUST roll back so the
    # later GuestAiChatLog INSERT below doesn't hit "current transaction is
    # aborted" (this was dropping every guest log from the admin review page).
    try:
        from app.services.memory_engine import maybe_store_memory, recall_memory
        from app.services.web_search import web_search
        await maybe_store_memory(db, "guest", device_id, payload.message, question=payload.message)
        guest_mem = await recall_memory(db, "guest", device_id, payload.message, top_k=2)
        if guest_mem:
            mem_text = "\n".join(f"- {m}" for m in guest_mem)
            messages.insert(1, {"role": "system", "content": f"이 사용자와의 이전 대화 메모리:\n{mem_text}"})
        else:
            web_results = await web_search(payload.message, max_results=2)
            if web_results:
                web_text = "\n".join(f"- {r['title']}: {r['content'][:250]}" for r in web_results if r.get("content"))
                messages.insert(1, {"role": "system", "content": f"최신 웹 정보:\n{web_text}"})
    except Exception as exc:
        try:
            await db.rollback()
        except Exception:
            pass
        logger.debug("guest_memory_web_failed", error=str(exc))

    # Short greeting/ack messages never need a reasoning pass shown to the
    # user -- force think_mode off regardless of the toggle sent from the
    # frontend. Logged with before/after so the improvement can be measured.
    effective_think_mode = payload.think_mode
    if effective_think_mode and should_skip_think_mode(payload.message):
        logger.info(
            "guest_ai_chat_think_mode_auto_skipped",
            ip=client_ip,
            requested_think_mode=True,
            effective_think_mode=False,
            content_length=len(payload.message),
        )
        effective_think_mode = False

    # The model always reasons internally regardless of budget -- think_mode
    # only controls whether that reasoning gets shown to the user, not how
    # much token budget the call gets.
    result = await _call_ollama_full(messages, max_tokens=_MAX_TOKENS)
    if result is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="AI 응답을 받아오지 못했습니다. 잠시 후 다시 시도해주세요.")
    reply, reasoning = result
    reply, confidence = extract_confidence(reply)
    # Defense in depth: strip any residual "I'm Gemma/Google" leak.
    reply = sanitize_identity(reply)
    reasoning = reasoning if effective_think_mode else None

    logger.info("guest_ai_chat_reply", ip=client_ip, device_id=device_id, confidence=confidence)

    # Best-effort persistence for admin visibility -- must never fail the
    # actual chat response (the guest already got their reply above).
    # Roll back first in case an earlier step (memory engine, web search)
    # left the session's transaction aborted — otherwise this INSERT is
    # rejected and admin sees zero guest conversations (seen live 2026-08-07).
    log_id = None
    try:
        try:
            await db.rollback()
        except Exception:
            pass
        log_entry = GuestAiChatLog(ip=client_ip, message=payload.message, reply=reply)
        db.add(log_entry)
        await db.commit()
        await db.refresh(log_entry)
        log_id = log_entry.id
    except Exception:
        logger.exception("guest_ai_chat_log_persist_failed")
        try:
            await db.rollback()
        except Exception:
            pass

    # Charge credits AFTER the AI call succeeded (1 credit = 1 char of input
    # + output). Failed calls never consume credits.
    estimated_chars = len(payload.message) + len(reply) + sum(len(m.content) for m in payload.history)
    ok, remaining = try_deduct_guest_credits(device_id, max(estimated_chars, 1))

    # Follow-up suggestions cost another model call — only generate them when
    # the guest explicitly asked (opt-in) so the common case stays fast.
    suggested = await _suggest_follow_ups(payload.message, reply) if payload.suggest_follow_ups else []

    return GuestChatResponse(
        reply=reply,
        log_id=log_id,
        reasoning=reasoning,
        confidence=confidence,
        remaining_credits=remaining if ok else 0,
        suggested_questions=suggested,
    )


def _build_guest_messages(payload: GuestChatRequest, mem_text: str | None = None, web_text: str | None = None) -> list[dict]:
    """Assemble the messages array shared by the JSON + streaming paths."""
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    if mem_text:
        messages.insert(1, {"role": "system", "content": f"이 사용자와의 이전 대화 메모리:\n{mem_text}"})
    elif web_text:
        messages.insert(1, {"role": "system", "content": f"최신 웹 정보:\n{web_text}"})
    messages.extend({"role": m.role, "content": m.content} for m in payload.history if m.role in ("user", "assistant"))
    messages.append({"role": "user", "content": payload.message})
    return messages


async def _load_guest_context(db: AsyncSession, device_id: str, question: str) -> tuple[str | None, str | None]:
    """Best-effort memory recall + web search fallback for a guest question.

    Returns (memory_text, web_text) — at most one is set. Never raises; on
    failure the DB transaction is rolled back so callers can persist logs.
    """
    try:
        from app.services.memory_engine import maybe_store_memory, recall_memory
        from app.services.web_search import web_search
        await maybe_store_memory(db, "guest", device_id, question, question=question)
        guest_mem = await recall_memory(db, "guest", device_id, question, top_k=2)
        if guest_mem:
            return "\n".join(f"- {m}" for m in guest_mem), None
        web_results = await web_search(question, max_results=2)
        if web_results:
            return None, "\n".join(f"- {r['title']}: {r['content'][:250]}" for r in web_results if r.get("content"))
    except Exception as exc:
        try:
            await db.rollback()
        except Exception:
            pass
        logger.debug("guest_memory_web_failed", error=str(exc))
    return None, None


@router.post("/chat/stream")
async def guest_chat_stream(payload: GuestChatRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    """SSE-streamed version of /chat — tokens arrive as they're generated so
    the guest sees the AI "thinking out loud" instead of a long spinner.

    Events:
      data: {"type":"chunk","content":"..."}     (partial reply text)
      data: {"type":"reasoning","content":"..."}  (think_mode only)
      data: {"type":"done","reply":"...","log_id":"...","confidence":"...","remaining_credits":N}
      data: {"type":"error","content":"..."}
    """
    from fastapi.responses import StreamingResponse

    client_ip = get_client_ip(request)
    device_id = _get_guest_device_id(request)

    if not settings.ollama_api_base:
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content="AI 서비스를 일시적으로 사용할 수 없습니다.")

    _set_guest_device_cookie(response, device_id)

    mem_text, web_text = await _load_guest_context(db, device_id, payload.message)
    messages = _build_guest_messages(payload, mem_text=mem_text, web_text=web_text)

    effective_think_mode = payload.think_mode
    if effective_think_mode and should_skip_think_mode(payload.message):
        effective_think_mode = False

    async def event_stream():
        import json
        from app.services.ai_core_service import _call_ollama_stream

        full_content = ""
        full_reasoning = ""
        error = None
        try:
            async for text, _usage in _call_ollama_stream(messages, max_tokens=_MAX_TOKENS):
                if text is None:
                    error = "AI 응답을 받아오지 못했습니다. 잠시 후 다시 시도해주세요."
                    break
                if not text:
                    continue
                if _is_reasoning_fragment(text):
                    full_reasoning += text
                    if effective_think_mode:
                        yield f"data: {json.dumps({'type': 'reasoning', 'content': text})}\n\n"
                else:
                    full_content += text
                    yield f"data: {json.dumps({'type': 'chunk', 'content': text})}\n\n"
        except Exception as exc:
            logger.error("guest_chat_stream_failed", error=str(exc))
            error = "AI 응답 생성 중 오류가 발생했습니다."

        if error:
            yield f"data: {json.dumps({'type': 'error', 'content': error})}\n\n"
            return

        # Post-process the completed reply exactly like /chat: strip the
        # confidence marker, enforce identity, sanitize leaked model names.
        reply, confidence = extract_confidence(full_content)
        reply = sanitize_identity(reply)

        logger.info("guest_ai_chat_reply", ip=client_ip, device_id=device_id, confidence=confidence)

        # Persist log (best-effort, never fail the stream) — roll back first
        # in case the memory engine aborted the transaction.
        log_id = None
        try:
            try:
                await db.rollback()
            except Exception:
                pass
            log_entry = GuestAiChatLog(ip=client_ip, message=payload.message, reply=reply)
            db.add(log_entry)
            await db.commit()
            await db.refresh(log_entry)
            log_id = log_entry.id
        except Exception:
            logger.exception("guest_ai_chat_log_persist_failed")
            try:
                await db.rollback()
            except Exception:
                pass

        estimated_chars = len(payload.message) + len(reply) + sum(len(m.content) for m in payload.history)
        ok, remaining = try_deduct_guest_credits(device_id, max(estimated_chars, 1))

        yield f"data: {json.dumps({
            'type': 'done',
            'reply': reply,
            'log_id': log_id,
            'confidence': confidence,
            'remaining_credits': remaining if ok else 0,
        })}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _is_reasoning_fragment(text: str) -> bool:
    """Heuristic: the Gemma 2 27B Ollama stream yields reasoning-ish text in
    special delimiters. Keep it cheap — anything between  thinking  tags."""
    t = text.strip()
    return t.startswith("<thinking") or t.startswith("</thinking") or t.startswith("<reasoning") or t.startswith("</reasoning")


async def _suggest_follow_ups(question: str, reply: str) -> list[str]:
    """Suggest 2 short follow-up questions based on the Q&A.

    Best-effort — never fail the response if this throws. Kept lightweight
    (single call, small max_tokens) so it adds little latency.
    """
    if not reply or len(reply) < 20:
        return []
    try:
        prompt = (
            "아래 질문과 답변을 바탕으로, 사용자가 이어서 물어볼 만한 "
            "짧은 질문 2개를 한국어로 제안해주세요.\n"
            f"질문: {question[:200]}\n"
            f"답변: {reply[:400]}\n\n"
            '형식: 각 질문을 "- "로 시작해서 줄바꿈으로 구분. '
            "질문만 출력하고 다른 말은 하지 마세요."
        )
        result = await _call_ollama_full(
            [{"role": "user", "content": prompt}],
            max_tokens=150,
        )
        if result is None:
            return []
        text, _ = result
        items = [line.strip().lstrip("-").strip() for line in text.splitlines() if line.strip()]
        return items[:3]
    except Exception as exc:
        logger.debug("guest_suggest_follow_ups_failed", error=str(exc))
        return []


class GuestFeedbackRequest(BaseModel):
    log_id: str = Field(min_length=1, max_length=36)
    thumbs_up: bool


@router.post("/feedback")
async def guest_feedback(payload: GuestFeedbackRequest, db: AsyncSession = Depends(get_db)):
    """Submit thumbs up/down feedback for a guest AI chat response.

    When positive feedback is received, also creates a Knowledge Candidate
    so the Q&A pair can be reviewed and potentially added to the KB.
    """
    from sqlalchemy import select, func
    from app.models.knowledge_base import KnowledgeCandidate

    try:
        await db.rollback()
    except Exception:
        pass
    result = await db.execute(
        select(GuestAiChatLog).where(GuestAiChatLog.id == payload.log_id).limit(1)
    )
    log = result.scalar_one_or_none()
    if log is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="로그를 찾을 수 없습니다.")

    log.thumbs_up = payload.thumbs_up
    await db.commit()

    # 게스트 긍정 피드백 → Knowledge Candidate 생성
    if payload.thumbs_up:
        try:
            # 중복 체크 (같은 질문+답변 조합)
            existing = await db.execute(
                select(KnowledgeCandidate).where(
                    KnowledgeCandidate.question == log.message[:500],
                    KnowledgeCandidate.answer == log.reply[:500],
                    KnowledgeCandidate.status == "pending",
                ).limit(1)
            )
            if existing.scalar_one_or_none() is None:
                # 게스트용 Knowledge Candidate 생성 (tenant_id는 "guest"로 설정)
                candidate = KnowledgeCandidate(
                    id=str(uuid.uuid4()),
                    tenant_id="guest",
                    question=log.message[:2000],
                    answer=log.reply[:5000],
                    feedback_score=5.0,  # 게스트는 thumbs_up만 있으므로 5점으로 설정
                    feedback_count=1,
                    model_name="ollama-chat",
                    tokens_used=0,
                    response_time_ms=0,
                    ai_version="guest",
                    prompt_version="v1",
                    status="pending",
                )
                db.add(candidate)
                await db.commit()
                logger.info("guest_knowledge_candidate_created", log_id=payload.log_id)
        except Exception as exc:
            logger.warning("guest_knowledge_candidate_failed", error=str(exc))

    logger.info("guest_ai_chat_feedback", log_id=payload.log_id, thumbs_up=payload.thumbs_up)
    return {"success": True}
