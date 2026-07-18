"""AI Reply 2.0 API Router.

Endpoints:
- Persona CRUD (create, read, update, delete, list)
- Suggestion generation (with context, memory, persona)
- Suggestion review workflow (approve/dismiss)
- Suggestion feedback
- Conversation context
- Auto-reply v2 settings
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
from app.core.logging import get_logger
from app.database import get_db
from app.schemas.ai_reply_v2 import (
    AutoReplyV2Settings,
    AutoReplyV2SettingsUpdate,
    ConversationRead,
    PersonaCreate,
    PersonaRead,
    PersonaUpdate,
    SuggestionFeedbackRequest,
    SuggestionGenerateRequest,
    SuggestionGenerateResponse,
    SuggestionRead,
    SuggestionReviewRequest,
)
from app.services.ai_reply_v2_service import (
    create_persona,
    delete_persona,
    generate_suggestions,
    get_active_persona,
    get_or_create_conversation,
    get_pending_auto_reply_suggestions,
    list_personas,
    list_suggestions,
    review_suggestion,
    submit_feedback,
    update_persona,
)

router = APIRouter(prefix="/api/ai-reply-v2", tags=["ai-reply-v2"])
logger = get_logger(__name__)


# ── Persona Endpoints ────────────────────────────────────────────────────


@router.get("/accounts/{account_id}/personas", response_model=list[PersonaRead])
async def get_personas(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """List all personas for an account."""
    await require_account_tenant_access(account_id, db, identity)
    return await list_personas(db, account_id)


@router.post("/accounts/{account_id}/personas", response_model=PersonaRead, status_code=status.HTTP_201_CREATED)
async def create_persona_endpoint(
    account_id: str,
    payload: PersonaCreate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Create a new persona for an account."""
    await require_account_tenant_access(account_id, db, identity)
    persona = await create_persona(db, identity.tenant_id, account_id, payload)
    logger.info("persona_created", account_id=account_id, persona_id=persona.id)
    return persona


@router.put("/accounts/{account_id}/personas/{persona_id}", response_model=PersonaRead)
async def update_persona_endpoint(
    account_id: str,
    persona_id: str,
    payload: PersonaUpdate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Update a persona."""
    await require_account_tenant_access(account_id, db, identity)
    persona = await update_persona(db, persona_id, account_id, payload)
    if persona is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Persona not found.")
    return persona


@router.delete("/accounts/{account_id}/personas/{persona_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_persona_endpoint(
    account_id: str,
    persona_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Delete a persona."""
    await require_account_tenant_access(account_id, db, identity)
    deleted = await delete_persona(db, persona_id, account_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Persona not found.")


@router.get("/accounts/{account_id}/personas/active", response_model=PersonaRead | None)
async def get_active_persona_endpoint(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get the active persona for an account."""
    await require_account_tenant_access(account_id, db, identity)
    return await get_active_persona(db, account_id)


# ── Suggestion Endpoints ─────────────────────────────────────────────────


@router.post("/suggestions", response_model=SuggestionGenerateResponse, status_code=status.HTTP_201_CREATED)
async def generate_suggestion(
    payload: SuggestionGenerateRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Generate AI reply suggestions for an incoming message.

    This is the main AI Reply 2.0 endpoint. It:
    1. Loads the account's active persona (or specified one)
    2. Builds conversation context from recent messages
    3. Searches Graphiti long-term memory for relevant context
    4. Generates 3 suggestions (primary + 2 alternatives) with confidence scores
    5. If auto_reply_enabled and confidence >= 0.85, auto-approves
    6. Stores everything in the conversation history
    """
    await require_account_tenant_access(payload.account_id, db, identity)

    suggestion = await generate_suggestions(db, identity.tenant_id, payload)
    if suggestion is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate reply suggestions. Please try again.",
        )

    return SuggestionGenerateResponse(
        id=suggestion.id,
        suggestions=suggestion.suggestions,
        context=suggestion.context,
        status=suggestion.status,
        response_time_ms=suggestion.response_time_ms,
    )


@router.get("/suggestions", response_model=list[SuggestionRead])
async def get_suggestions(
    account_id: str = Query(..., description="Account ID"),
    status_filter: str | None = Query(None, alias="status", description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """List suggestions for an account."""
    await require_account_tenant_access(account_id, db, identity)
    return await list_suggestions(db, account_id, status=status_filter, limit=limit, offset=offset)


@router.get("/suggestions/{suggestion_id}", response_model=SuggestionRead)
async def get_suggestion(
    suggestion_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get a specific suggestion by ID."""
    from app.models.ai_reply_v2 import AiReplySuggestionV2
    from sqlalchemy import select

    result = await db.execute(
        select(AiReplySuggestionV2).where(AiReplySuggestionV2.id == suggestion_id).limit(1)
    )
    suggestion = result.scalar_one_or_none()
    if suggestion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Suggestion not found.")
    await require_account_tenant_access(suggestion.account_id, db, identity)
    return suggestion


@router.post("/suggestions/{suggestion_id}/review", response_model=SuggestionRead)
async def review_suggestion_endpoint(
    suggestion_id: str,
    payload: SuggestionReviewRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Review (approve/dismiss) a suggestion."""
    from app.models.ai_reply_v2 import AiReplySuggestionV2
    from sqlalchemy import select

    result = await db.execute(
        select(AiReplySuggestionV2).where(AiReplySuggestionV2.id == suggestion_id).limit(1)
    )
    suggestion = result.scalar_one_or_none()
    if suggestion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Suggestion not found.")
    await require_account_tenant_access(suggestion.account_id, db, identity)

    reviewed = await review_suggestion(
        db, suggestion_id, suggestion.account_id, identity.user_id or "system", payload,
    )
    if reviewed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Suggestion not found.")
    return reviewed


@router.post("/suggestions/{suggestion_id}/feedback", response_model=SuggestionRead)
async def feedback_suggestion(
    suggestion_id: str,
    payload: SuggestionFeedbackRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Submit feedback on a suggestion."""
    from app.models.ai_reply_v2 import AiReplySuggestionV2
    from sqlalchemy import select

    result = await db.execute(
        select(AiReplySuggestionV2).where(AiReplySuggestionV2.id == suggestion_id).limit(1)
    )
    suggestion = result.scalar_one_or_none()
    if suggestion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Suggestion not found.")
    await require_account_tenant_access(suggestion.account_id, db, identity)

    updated = await submit_feedback(db, suggestion_id, suggestion.account_id, payload)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Suggestion not found.")
    return updated


# ── Conversation Context ─────────────────────────────────────────────────


@router.get("/accounts/{account_id}/conversations/{chat_id}", response_model=ConversationRead)
async def get_conversation(
    account_id: str,
    chat_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get conversation context for a chat."""
    await require_account_tenant_access(account_id, db, identity)
    conv = await get_or_create_conversation(db, identity.tenant_id, account_id, chat_id)
    return conv


# ── Auto-Reply Settings ──────────────────────────────────────────────────


@router.get("/accounts/{account_id}/settings", response_model=AutoReplyV2Settings)
async def get_auto_reply_settings(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get AI Reply 2.0 settings for an account."""
    from app.crud import account as account_crud

    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.")

    active_persona = await get_active_persona(db, account_id)
    return AutoReplyV2Settings(
        account_id=account_id,
        auto_reply_enabled=getattr(account, "auto_reply_enabled", False),
        ai_fallback_enabled=getattr(account, "ai_fallback_reply_enabled", False),
        min_confidence=0.85,
        max_replies_per_day=50,
        active_persona_id=active_persona.id if active_persona else None,
    )


@router.patch("/accounts/{account_id}/settings", response_model=AutoReplyV2Settings)
async def update_auto_reply_settings(
    account_id: str,
    payload: AutoReplyV2SettingsUpdate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Update AI Reply 2.0 settings for an account."""
    from app.crud import account as account_crud

    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.")

    if payload.auto_reply_enabled is not None:
        account.auto_reply_enabled = payload.auto_reply_enabled
    if payload.ai_fallback_enabled is not None:
        account.ai_fallback_reply_enabled = payload.ai_fallback_enabled
    await db.commit()

    active_persona = await get_active_persona(db, account_id)
    return AutoReplyV2Settings(
        account_id=account_id,
        auto_reply_enabled=account.auto_reply_enabled,
        ai_fallback_enabled=getattr(account, "ai_fallback_reply_enabled", False),
        min_confidence=payload.min_confidence or 0.85,
        max_replies_per_day=payload.max_replies_per_day or 50,
        active_persona_id=active_persona.id if active_persona else None,
    )


@router.get("/accounts/{account_id}/auto-replies/pending", response_model=list[SuggestionRead])
async def get_pending_auto_replies(
    account_id: str,
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get pending auto-reply suggestions that were automatically sent."""
    await require_account_tenant_access(account_id, db, identity)
    return await get_pending_auto_reply_suggestions(db, account_id, limit=limit)