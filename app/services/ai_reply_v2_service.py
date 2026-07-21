"""AI Reply 2.0 Service.

Core service for:
- Persona/tone management
- Conversation context tracking
- Graphiti long-term memory integration
- Multi-suggestion generation with confidence scoring
- Auto-reply workflow (suggest → auto-send if confidence high)
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, desc, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.models.ai_reply_v2 import (
    AiReplyPersona,
    AiReplyConversation,
    AiReplySuggestionV2,
)
from app.schemas.ai_reply_v2 import (
    PersonaCreate,
    PersonaUpdate,
    SuggestionGenerateRequest,
    SuggestionReviewRequest,
    SuggestionFeedbackRequest,
)
from app.services.ai_core_service import call_deepseek, search_memory, store_memory

logger = get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

_MAX_CONVERSATION_MESSAGES = 20
_MAX_SUGGESTION_INPUT = 4000
_DEFAULT_MAX_TOKENS = 800
_AUTO_REPLY_CONFIDENCE_THRESHOLD = 0.85

# ── System Prompt Templates ──────────────────────────────────────────────

_SYSTEM_PROMPT_BASE = (
    "You are TeleMon AI Reply Assistant v2.0. Your role is to generate "
    "natural, context-aware reply suggestions for incoming Telegram messages.\n\n"
    "Rules:\n"
    "- Respond ONLY with valid JSON, no other text\n"
    "- Always provide 3 suggestions: primary (best) + 2 alternatives\n"
    "- Each suggestion must be realistic and ready to send\n"
    "- Consider conversation history and user relationship\n"
    "- Match the specified tone and style precisely\n"
    "- Keep replies concise (under {max_length} chars)\n"
    "- Use {language} language\n\n"
    "Output format:\n"
    '{{\n'
    '  "primary": {{\n'
    '    "text": "best reply",\n'
    '    "confidence": 0.0-1.0,\n'
    '    "reason": "why this fits"\n'
    '  }},\n'
    '  "alternatives": [\n'
    '    {{"text": "...", "confidence": 0.0-1.0, "reason": "..."}},\n'
    '    {{"text": "...", "confidence": 0.0-1.0, "reason": "..."}}\n'
    '  ]\n'
    '}}\n\n'
    "Tone: {tone}\n"
    "Style: {style_json}\n"
    "Business context: {business_json}\n"
)

_TONE_DESCRIPTIONS = {
    "professional": "Professional and courteous. Use formal language, be precise and respectful.",
    "casual": "Casual and relaxed. Use everyday language, be approachable.",
    "friendly": "Friendly and warm. Show genuine interest, be personable.",
    "formal": "Highly formal. Use honorifics, be very polite and structured.",
    "witty": "Witty and clever. Use humor appropriately, be engaging.",
    "empathetic": "Empathetic and understanding. Show emotional intelligence.",
    "concise": "Concise and direct. Get to the point quickly, be efficient.",
    "enthusiastic": "Enthusiastic and energetic. Show excitement, be positive.",
}


# ── Persona Management ───────────────────────────────────────────────────


async def create_persona(
    db: AsyncSession,
    tenant_id: str,
    account_id: str,
    payload: PersonaCreate,
) -> AiReplyPersona:
    """Create a new persona for an account."""
    persona = AiReplyPersona(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        account_id=account_id,
        name=payload.name,
        tone=payload.tone,
        style=payload.style.model_dump() if payload.style else {},
        business_info=payload.business_info.model_dump() if payload.business_info else {},
    )

    # If this is the first persona, make it active
    existing = await db.execute(
        select(AiReplyPersona).where(
            AiReplyPersona.account_id == account_id,
            AiReplyPersona.is_active == True,
        ).limit(1)
    )
    if existing.scalar_one_or_none() is None:
        persona.is_active = True

    db.add(persona)
    await db.commit()
    await db.refresh(persona)
    logger.info("ai_reply_persona_created", account_id=account_id, persona_id=persona.id)
    return persona


async def update_persona(
    db: AsyncSession,
    persona_id: str,
    account_id: str,
    payload: PersonaUpdate,
) -> AiReplyPersona | None:
    """Update a persona."""
    result = await db.execute(
        select(AiReplyPersona).where(
            AiReplyPersona.id == persona_id,
            AiReplyPersona.account_id == account_id,
        )
    )
    persona = result.scalar_one_or_none()
    if persona is None:
        return None

    if payload.name is not None:
        persona.name = payload.name
    if payload.tone is not None:
        persona.tone = payload.tone
    if payload.style is not None:
        persona.style = payload.style.model_dump()
    if payload.business_info is not None:
        persona.business_info = payload.business_info.model_dump()
    if payload.is_active is not None:
        # Deactivate all other personas for this account
        if payload.is_active:
            await db.execute(
                AiReplyPersona.__table__.update()
                .where(
                    AiReplyPersona.account_id == account_id,
                    AiReplyPersona.id != persona_id,
                )
                .values(is_active=False)
            )
        persona.is_active = payload.is_active

    await db.commit()
    await db.refresh(persona)
    logger.info("ai_reply_persona_updated", persona_id=persona_id)
    return persona


async def list_personas(
    db: AsyncSession,
    account_id: str,
) -> list[AiReplyPersona]:
    """List all personas for an account."""
    result = await db.execute(
        select(AiReplyPersona)
        .where(AiReplyPersona.account_id == account_id)
        .order_by(AiReplyPersona.created_at.desc())
    )
    return list(result.scalars().all())


async def get_active_persona(
    db: AsyncSession,
    account_id: str,
) -> AiReplyPersona | None:
    """Get the active persona for an account."""
    result = await db.execute(
        select(AiReplyPersona).where(
            AiReplyPersona.account_id == account_id,
            AiReplyPersona.is_active == True,
        ).limit(1)
    )
    return result.scalar_one_or_none()


async def delete_persona(
    db: AsyncSession,
    persona_id: str,
    account_id: str,
) -> bool:
    """Delete a persona."""
    result = await db.execute(
        select(AiReplyPersona).where(
            AiReplyPersona.id == persona_id,
            AiReplyPersona.account_id == account_id,
        )
    )
    persona = result.scalar_one_or_none()
    if persona is None:
        return False
    await db.delete(persona)
    await db.commit()
    logger.info("ai_reply_persona_deleted", persona_id=persona_id)
    return True


# ── Conversation Context ─────────────────────────────────────────────────


async def get_or_create_conversation(
    db: AsyncSession,
    tenant_id: str,
    account_id: str,
    chat_id: str,
    chat_title: str | None = None,
) -> AiReplyConversation:
    """Get or create a conversation context record."""
    result = await db.execute(
        select(AiReplyConversation).where(
            AiReplyConversation.account_id == account_id,
            AiReplyConversation.chat_id == chat_id,
        ).limit(1)
    )
    conv = result.scalar_one_or_none()
    if conv is None:
        conv = AiReplyConversation(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            account_id=account_id,
            chat_id=chat_id,
            chat_title=chat_title,
            messages=[],
            message_count=0,
        )
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
    return conv


async def add_message_to_conversation(
    db: AsyncSession,
    conversation: AiReplyConversation,
    role: str,
    content: str,
    message_id: int | None = None,
) -> None:
    """Add a message to the conversation history and prune if needed."""
    now = datetime.now(timezone.utc).isoformat()
    msg = {
        "role": role,
        "content": content,
        "timestamp": now,
    }
    if message_id is not None:
        msg["message_id"] = message_id

    messages = list(conversation.messages or [])
    messages.insert(0, msg)  # newest first

    # Prune to max size
    if len(messages) > _MAX_CONVERSATION_MESSAGES:
        messages = messages[:_MAX_CONVERSATION_MESSAGES]

    conversation.messages = messages
    conversation.message_count = len(messages)
    conversation.last_message_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()


async def build_conversation_context(
    db: AsyncSession,
    tenant_id: str,
    account_id: str,
    chat_id: str,
    incoming_message: str,
) -> tuple[list[dict], str | None]:
    """Build conversation context for the AI prompt.

    Returns (recent_messages, summary).
    """
    conv = await get_or_create_conversation(db, tenant_id, account_id, chat_id)
    messages = list(conv.messages or [])

    # Build message list for prompt (oldest first)
    recent_messages = list(reversed(messages[:_MAX_CONVERSATION_MESSAGES // 2]))

    # Auto-generate summary if we have enough messages and no summary yet
    if conv.message_count >= 6 and conv.summary is None:
        summary = await _generate_conversation_summary(db, conv, recent_messages)
        if summary:
            conv.summary = summary
            conv.summary_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await db.commit()

    return recent_messages, conv.summary


async def _generate_conversation_summary(
    db: AsyncSession,
    conversation: AiReplyConversation,
    recent_messages: list[dict],
) -> str | None:
    """Generate a summary of the conversation for long-term context."""
    if not recent_messages:
        return None

    messages_text = "\n".join(
        f"{m['role']}: {m['content'][:200]}"
        for m in recent_messages[-10:]
    )

    prompt = [
        {
            "role": "system",
            "content": (
                "Summarize this conversation in 2-3 sentences in Korean. "
                "Focus on: who is talking, what is the relationship, "
                "what topics are being discussed, any key information shared. "
                "Respond with ONLY the summary text."
            ),
        },
        {"role": "user", "content": f"Conversation:\n{messages_text}"},
    ]

    reply, _, _ = await call_deepseek(prompt, max_tokens=200)
    return reply


# ── Memory Integration ───────────────────────────────────────────────────


async def enrich_with_memory(
    tenant_id: str,
    account_id: str,
    chat_id: str,
    incoming_message: str,
) -> list[str]:
    """Search Graphiti memory for relevant context about this chat/user."""
    try:
        # Search for relevant memories
        memory_results = await search_memory(
            tenant_id,
            f"chat {chat_id} {incoming_message[:200]}",
            max_results=3,
        )
        if memory_results:
            return [
                m.get("fact", m.get("content", ""))
                for m in memory_results
                if m.get("fact") or m.get("content")
            ]
    except Exception as exc:
        logger.warning("memory_search_failed", error=str(exc))
    return []


async def store_reply_memory(
    tenant_id: str,
    account_id: str,
    chat_id: str,
    incoming_message: str,
    reply_text: str,
    user_id: str | None = None,
) -> None:
    """Store the interaction in Graphiti long-term memory."""
    try:
        episode = (
            f"Account {account_id} received message from chat {chat_id}"
            f"{f' (user {user_id})' if user_id else ''}: "
            f'"{incoming_message[:300]}". '
            f'Reply sent: "{reply_text[:300]}".'
        )
        await store_memory(
            tenant_id,
            f"reply:{account_id}:{chat_id}",
            episode,
            source="message",
            source_description="AI Reply 2.0 interaction",
        )
    except Exception as exc:
        logger.warning("memory_store_failed", error=str(exc))


# ── Suggestion Generation ────────────────────────────────────────────────


async def generate_suggestions(
    db: AsyncSession,
    tenant_id: str,
    request: SuggestionGenerateRequest,
) -> AiReplySuggestionV2 | None:
    """Generate reply suggestions for an incoming message.

    This is the main entry point for AI Reply 2.0.
    """
    start_time = time.monotonic()

    # 1. Get persona
    persona = None
    if request.persona_id:
        result = await db.execute(
            select(AiReplyPersona).where(
                AiReplyPersona.id == request.persona_id,
                AiReplyPersona.account_id == request.account_id,
            ).limit(1)
        )
        persona = result.scalar_one_or_none()
    else:
        persona = await get_active_persona(db, request.account_id)

    # 2. Build conversation context
    recent_messages, conversation_summary = await build_conversation_context(
        db, tenant_id, request.account_id, request.chat_id, request.incoming_message,
    )

    # 3. Search memory
    memory_context = await enrich_with_memory(
        tenant_id, request.account_id, request.chat_id, request.incoming_message,
    )

    # 4. Build system prompt
    tone_name = persona.tone if persona else "professional"
    tone_desc = _TONE_DESCRIPTIONS.get(tone_name, _TONE_DESCRIPTIONS["professional"])
    style_json = json.dumps(persona.style if persona else {}, ensure_ascii=False)
    business_json = json.dumps(persona.business_info if persona else {}, ensure_ascii=False)
    max_length = (persona.style or {}).get("max_length", 500) if persona else 500
    language = (persona.style or {}).get("language", "ko") if persona else "ko"

    system_prompt = _SYSTEM_PROMPT_BASE.format(
        tone=tone_desc,
        style_json=style_json,
        business_json=business_json,
        max_length=max_length,
        language=language,
    )

    # 5. Build messages array
    messages = [{"role": "system", "content": system_prompt}]

    # Add conversation summary if available
    if conversation_summary:
        messages.append({
            "role": "system",
            "content": f"Conversation context: {conversation_summary}",
        })

    # Add memory context if available
    if memory_context:
        memory_text = "\n".join(f"- {m}" for m in memory_context)
        messages.append({
            "role": "system",
            "content": f"Relevant history from memory:\n{memory_text}",
        })

    # Add recent messages
    for msg in recent_messages[-6:]:  # Last 6 messages for context
        messages.append({
            "role": msg["role"],
            "content": msg["content"][:500],
        })

    # Add incoming message
    messages.append({
        "role": "user",
        "content": f"Incoming message from {request.user_name or 'user'}: {request.incoming_message[:2000]}",
    })

    # 6. Call DeepSeek
    reply_text, tokens_used, _ = await call_deepseek(messages, max_tokens=_DEFAULT_MAX_TOKENS)
    if reply_text is None:
        logger.error("ai_reply_v2_generation_failed", account_id=request.account_id)
        return None

    # 7. Parse JSON response
    suggestions = _parse_suggestions(reply_text)
    if suggestions is None:
        logger.error("ai_reply_v2_parse_failed", account_id=request.account_id)
        return None

    # 8. Build context metadata
    context = {
        "persona_id": persona.id if persona else None,
        "persona_name": persona.name if persona else None,
        "tone": tone_name,
        "conversation_summary": conversation_summary,
        "memory_context": memory_context,
        "recent_messages": len(recent_messages),
    }

    # 9. Save suggestion
    elapsed_ms = int((time.monotonic() - start_time) * 1000)
    suggestion = AiReplySuggestionV2(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        account_id=request.account_id,
        chat_id=request.chat_id,
        chat_title=request.chat_title,
        user_id=request.user_id,
        user_name=request.user_name,
        incoming_message=request.incoming_message,
        suggestions=suggestions,
        context=context,
        status="pending",
        auto_reply_enabled=request.auto_reply_enabled,
        response_time_ms=elapsed_ms,
    )

    # 10. Auto-reply workflow: if confidence is high enough, auto-send
    if request.auto_reply_enabled:
        primary_confidence = suggestions.get("primary", {}).get("confidence", 0)
        if primary_confidence >= _AUTO_REPLY_CONFIDENCE_THRESHOLD:
            suggestion.status = "approved"
            suggestion.auto_reply_sent = True
            suggestion.auto_reply_sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
            suggestion.selected_suggestion = "primary"
            logger.info(
                "ai_reply_v2_auto_sent",
                account_id=request.account_id,
                confidence=primary_confidence,
            )

    db.add(suggestion)

    # 11. Update conversation context
    conv = await get_or_create_conversation(
        db, tenant_id, request.account_id, request.chat_id, request.chat_title,
    )
    await add_message_to_conversation(db, conv, "user", request.incoming_message)
    primary_text = suggestions.get("primary", {}).get("text", "")
    if primary_text:
        await add_message_to_conversation(db, conv, "assistant", primary_text)

    # 12. Store in long-term memory
    if primary_text:
        await store_reply_memory(
            tenant_id, request.account_id, request.chat_id,
            request.incoming_message, primary_text, request.user_id,
        )

    await db.commit()
    await db.refresh(suggestion)

    logger.info(
        "ai_reply_v2_suggestion_created",
        account_id=request.account_id,
        suggestion_id=suggestion.id,
        tokens_used=tokens_used,
        elapsed_ms=elapsed_ms,
    )
    return suggestion


def _parse_suggestions(raw_text: str) -> dict[str, Any] | None:
    """Parse the JSON response from DeepSeek into structured suggestions."""
    # Try to extract JSON from the response
    text = raw_text.strip()

    # Remove markdown code blocks if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Find the first and last ```
        start = 0
        end = len(lines)
        for i, line in enumerate(lines):
            if line.strip().startswith("```"):
                if start == 0:
                    start = i + 1
                else:
                    end = i
                    break
        text = "\n".join(lines[start:end]).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        import re
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return None
        else:
            return None

    # Validate structure
    if "primary" not in data:
        return None

    primary = data["primary"]
    if not isinstance(primary, dict) or "text" not in primary:
        return None

    # Ensure alternatives exist
    if "alternatives" not in data:
        data["alternatives"] = []

    # Normalize confidence scores
    for key in ["primary"] + [f"alternatives[{i}]" for i in range(len(data.get("alternatives", [])))]:
        if key == "primary":
            item = data["primary"]
        else:
            idx = int(key.split("[")[1].split("]")[0])
            item = data["alternatives"][idx]
        if isinstance(item, dict):
            item["confidence"] = max(0.0, min(1.0, float(item.get("confidence", 0.5))))

    return {
        "primary": data["primary"],
        "alternatives": data.get("alternatives", [])[:3],  # Max 3 alternatives
    }


# ── Suggestion Review Workflow ───────────────────────────────────────────


async def review_suggestion(
    db: AsyncSession,
    suggestion_id: str,
    account_id: str,
    reviewer: str,
    payload: SuggestionReviewRequest,
) -> AiReplySuggestionV2 | None:
    """Review and approve/dismiss a suggestion."""
    result = await db.execute(
        select(AiReplySuggestionV2).where(
            AiReplySuggestionV2.id == suggestion_id,
            AiReplySuggestionV2.account_id == account_id,
        ).limit(1)
    )
    suggestion = result.scalar_one_or_none()
    if suggestion is None:
        return None

    suggestion.status = payload.status
    suggestion.reviewed_by = reviewer
    suggestion.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)

    if payload.selected_suggestion:
        suggestion.selected_suggestion = payload.selected_suggestion
    if payload.custom_reply:
        suggestion.custom_reply = payload.custom_reply

    await db.commit()
    await db.refresh(suggestion)
    logger.info(
        "ai_reply_v2_suggestion_reviewed",
        suggestion_id=suggestion_id,
        status=payload.status,
    )
    return suggestion


async def submit_feedback(
    db: AsyncSession,
    suggestion_id: str,
    account_id: str,
    payload: SuggestionFeedbackRequest,
) -> AiReplySuggestionV2 | None:
    """Submit feedback on a suggestion."""
    result = await db.execute(
        select(AiReplySuggestionV2).where(
            AiReplySuggestionV2.id == suggestion_id,
            AiReplySuggestionV2.account_id == account_id,
        ).limit(1)
    )
    suggestion = result.scalar_one_or_none()
    if suggestion is None:
        return None

    suggestion.feedback = payload.model_dump()
    await db.commit()
    await db.refresh(suggestion)
    return suggestion


async def list_suggestions(
    db: AsyncSession,
    account_id: str,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AiReplySuggestionV2]:
    """List suggestions for an account."""
    query = (
        select(AiReplySuggestionV2)
        .where(AiReplySuggestionV2.account_id == account_id)
        .order_by(desc(AiReplySuggestionV2.created_at))
        .offset(offset)
        .limit(limit)
    )
    if status:
        query = query.where(AiReplySuggestionV2.status == status)

    result = await db.execute(query)
    return list(result.scalars().all())


async def get_pending_auto_reply_suggestions(
    db: AsyncSession,
    account_id: str,
    limit: int = 10,
) -> list[AiReplySuggestionV2]:
    """Get pending suggestions that were auto-reply candidates."""
    result = await db.execute(
        select(AiReplySuggestionV2)
        .where(
            AiReplySuggestionV2.account_id == account_id,
            AiReplySuggestionV2.auto_reply_enabled == True,
            AiReplySuggestionV2.auto_reply_sent == True,
        )
        .order_by(desc(AiReplySuggestionV2.created_at))
        .limit(limit)
    )
    return list(result.scalars().all())