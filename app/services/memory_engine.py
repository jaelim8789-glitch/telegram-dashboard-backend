"""Auto Memory Engine — selective long-term memory.

Evaluates whether a conversation fragment is worth remembering (memory_score),
classifies it into a category, dedupes against existing entries (embedding
similarity → update instead of duplicate), and stores it. Works for both
members (tenant_id) and guests (owner_key = IP).

LLM-independent: swapping the underlying model keeps this engine intact.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.memory import MemoryEntry
from app.services.ai_core_service import call_ollama
from app.services.knowledge_base import embed_texts

logger = get_logger(__name__)

MEMORY_SAVE_THRESHOLD = 90
MEMORY_CATEGORIES = [
    "user_profile", "preferences", "projects", "company",
    "goals", "long_term_facts", "conversation_insights",
]

# Embedding similarity above which we treat as "same memory" → update.
DEDUPE_COSINE_THRESHOLD = 0.85


async def evaluate_memory_value(content: str, question: str = "") -> tuple[int, str]:
    """Evaluate memory value (0-100) + category.

    Uses the LLM when available, but always combines with a keyword heuristic
    and returns the MAX score — so clear company/preference signals are never
    lost to an LLM that returned 0 / failed to parse.
    """
    llm_score = 0
    llm_cat = "conversation_insights"

    prompt = (
        "대화 내용이 장기 기억으로 저장할 가치가 있는지 0~100 점수로 평가하고, "
        "카테고리를 정해주세요.\n\n"
        "평가 기준:\n"
        "- 장기적으로 중요한 정보인가\n"
        "- 사용자의 선호인가\n"
        "- 프로젝트/회사 정보인가\n"
        "- 반복적으로 등장하는 정보인가\n"
        "- 미래 대화에 도움이 되는가\n\n"
        f"질문: {question[:200]}\n"
        f"내용: {content[:500]}\n\n"
        "응답 형식 (JSON만): {\"score\": 0~100, \"category\": "
        "\"" + "|".join(MEMORY_CATEGORIES) + "\"}\n"
        "일상적인 대화(예: '오늘 배고프다')는 score를 낮게 주세요."
    )
    try:
        reply, _, _ = await call_ollama(
            [{"role": "user", "content": prompt}], max_tokens=120,
        )
        if reply:
            import re
            import json
            m = re.search(r'\{.*\}', reply, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                llm_score = int(data.get("score", 0))
                llm_cat = data.get("category", "conversation_insights")
                if llm_cat not in MEMORY_CATEGORIES:
                    llm_cat = "conversation_insights"
            else:
                sm = re.search(r'score[:\s]*(\d+)', reply, re.IGNORECASE)
                cm = re.search(r'category[:\s]*"?([a-z_]+)"?', reply, re.IGNORECASE)
                if sm:
                    llm_score = int(sm.group(1))
                if cm and cm.group(1) in MEMORY_CATEGORIES:
                    llm_cat = cm.group(1)
    except Exception as exc:
        logger.debug("memory_eval_llm_failed", error=str(exc))

    # Keyword heuristic (always runs) — clear signals boost the score.
    heur_score, heur_cat = _heuristic_memory_score(content)
    if heur_score >= llm_score:
        return min(100, heur_score), heur_cat
    return max(0, min(100, llm_score)), llm_cat


def _heuristic_memory_score(content: str) -> tuple[int, str]:
    """Cheap keyword-based memory value + category (no LLM)."""
    t = (content or "").strip()
    signals = 0
    cat = "conversation_insights"
    for kw in ("우리 회사", "회사는", "이름은", "사장님", "대표님", "브랜드", "TeleMon"):
        if kw in t:
            signals += 2
            cat = "company"
    for kw in ("반말", "말투", "~로 해줘", "선호", "좋아해", "~하게", "해줘", "하세요"):
        if kw in t:
            signals += 2
            cat = "preferences"
            break  # single clear preference signal is enough
    for kw in ("프로젝트", "진행 중", "개발 중", "만들고 있어"):
        if kw in t:
            signals += 2
            cat = "projects"
    for kw in ("목표", "목표는", "하고 싶어", "되기 위해"):
        if kw in t:
            signals += 2
            cat = "goals"
    if signals >= 2 and len(t) > 8:
        return min(95, 60 + signals * 10), cat
    # Short, unambiguous preference ("반말로 대답해") — save as preference.
    if cat == "preferences" and len(t) > 3:
        return 92, cat
    return 0, cat


async def _find_similar(db: AsyncSession, owner_type: str, owner_key: str, query_emb: list[float]) -> MemoryEntry | None:
    """Find existing memory with high embedding similarity (dedupe)."""
    try:
        from sqlalchemy import text
        emb = ",".join(str(round(x, 6)) for x in query_emb)
        rows = (await db.execute(
            text(
                "SELECT id, content, 1 - (embedding <=> :q::vector) AS sim "
                "FROM ai_memories WHERE owner_type = :ot AND owner_key = :ok "
                "AND embedding IS NOT NULL ORDER BY embedding <=> :q::vector LIMIT 1"
            ),
            {"q": emb, "ot": owner_type, "ok": owner_key},
        )).all()
        if rows and rows[0].sim >= DEDUPE_COSINE_THRESHOLD:
            return (await db.execute(
                select(MemoryEntry).where(MemoryEntry.id == rows[0].id).limit(1)
            )).scalar_one_or_none()
    except Exception as exc:
        logger.debug("memory_dedupe_failed", error=str(exc))
    return None


async def maybe_store_memory(
    db: AsyncSession,
    owner_type: str,
    owner_key: str,
    content: str,
    question: str = "",
) -> dict:
    """Evaluate + dedupe + store one memory fragment.

    Returns {"stored": bool, "score": int, "category": str, "updated": bool}.
    """
    score, category = await evaluate_memory_value(content, question)
    if score < MEMORY_SAVE_THRESHOLD:
        return {"stored": False, "score": score, "category": category, "updated": False}

    # Embed for dedupe (best-effort; fall back to storing without embedding).
    emb = None
    try:
        emb = (await embed_texts([content]))[0]
    except Exception as exc:
        logger.debug("memory_embed_failed", error=str(exc))

    if emb:
        existing = await _find_similar(db, owner_type, owner_key, emb)
        if existing:
            existing.content = content
            existing.memory_score = score
            existing.category = category
            existing.embedding = emb
            existing.source_question = question or existing.source_question
            await db.commit()
            return {"stored": True, "score": score, "category": category, "updated": True}

    entry = MemoryEntry(
        id=str(uuid.uuid4()),
        owner_type=owner_type,
        owner_key=owner_key,
        category=category,
        content=content,
        memory_score=score,
        embedding=emb,
        source_question=question or None,
    )
    db.add(entry)
    await db.commit()
    return {"stored": True, "score": score, "category": category, "updated": False}


async def recall_memory(db: AsyncSession, owner_type: str, owner_key: str, query: str, top_k: int = 3) -> list[str]:
    """Retrieve relevant stored memories for the owner (cosine search)."""
    try:
        qemb = (await embed_texts([query]))[0]
        from sqlalchemy import text
        emb = ",".join(str(round(x, 6)) for x in qemb)
        rows = (await db.execute(
            text(
                "SELECT content FROM ai_memories WHERE owner_type = :ot AND owner_key = :ok "
                "AND embedding IS NOT NULL ORDER BY embedding <=> :q::vector LIMIT :k"
            ),
            {"q": emb, "ot": owner_type, "ok": owner_key, "k": top_k},
        )).all()
        return [r.content for r in rows]
    except Exception as exc:
        logger.debug("memory_recall_failed", error=str(exc))
        return []


async def memory_stats(db: AsyncSession, owner_type: str | None = None, owner_key: str | None = None) -> dict:
    """Analytics: memory count by category, avg score, etc."""
    from sqlalchemy import func

    stmt = select(MemoryEntry.category, func.count(MemoryEntry.id), func.avg(MemoryEntry.memory_score))
    if owner_type and owner_key:
        stmt = stmt.where(MemoryEntry.owner_type == owner_type, MemoryEntry.owner_key == owner_key)
    stmt = stmt.group_by(MemoryEntry.category)
    rows = (await db.execute(stmt)).all()
    total = sum(r[1] for r in rows)
    avg = round(sum((r[2] or 0) * r[1] for r in rows) / total, 1) if total else 0
    return {"total": total, "avg_score": avg, "by_category": {r[0]: r[1] for r in rows}}
