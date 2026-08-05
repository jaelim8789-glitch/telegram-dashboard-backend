"""Knowledge Base: ingestion, embedding, hybrid search, RAG."""

import logging
import uuid
from typing import Any

import httpx
import tiktoken
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as cfg

settings = cfg
from app.models.knowledge_base import Chunk, Document, Feedback, SearchLog
from app.schemas.knowledge_base import SearchResult

logger = logging.getLogger(__name__)

enc = tiktoken.get_encoding("cl100k_base")

CHUNK_SIZE = 512
CHUNK_OVERLAP = 64


# ── Ingestion ─────────────────────────────────────────────────


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    tokens = enc.encode(text)
    chunks = []
    i = 0
    while i < len(tokens):
        chunk_tokens = tokens[i : i + chunk_size]
        chunks.append(enc.decode(chunk_tokens))
        i += chunk_size - overlap
    return chunks


async def embed_texts(texts: list[str]) -> list[list[float]]:
    if not settings.kb_openai_api_key:
        logger.warning("kb_openai_api_key not set — returning zero embeddings")
        return [[0.0] * 1536 for _ in texts]
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{settings.kb_openai_base_url}/embeddings",
            json={"model": settings.kb_embedding_model, "input": texts},
            headers={"Authorization": f"Bearer {settings.kb_openai_api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return [d["embedding"] for d in data["data"]]


async def ingest_document(db: AsyncSession, title: str, content: str, collection: str = "general",
                          source_url: str | None = None, permission_groups: list[str] | None = None,
                          metadata: dict | None = None, user_id: str | None = None,
                          source_type: str = "manual") -> Document:
    doc = Document(
        title=title, content=content, source_url=source_url,
        collection=collection, permission_groups=permission_groups or [],
        extra=metadata or {}, created_by=user_id, source_type=source_type,
    )
    db.add(doc)
    await db.flush()

    # Chunk & embed
    raw_chunks = chunk_text(content)
    embeddings = await embed_texts(raw_chunks)

    for idx, (chunk_text_content, embedding) in enumerate(zip(raw_chunks, embeddings)):
        chunk = Chunk(
            document_id=doc.id,
            content=chunk_text_content,
            chunk_index=idx,
            token_count=len(enc.encode(chunk_text_content)),
            embedding=embedding,
        )
        db.add(chunk)

    await db.commit()
    await db.refresh(doc)
    logger.info("Ingested doc=%s title=%s chunks=%d", doc.id, title, len(raw_chunks))
    return doc


# ── Search ────────────────────────────────────────────────────


async def search_knowledge_base(db: AsyncSession, query: str, top_k: int = 5,
                                collection: str | None = None) -> tuple[list[SearchResult], list[str]]:
    """Hybrid search: vector cosine + keyword (FTS) + RRF fusion."""
    query_embedding = (await embed_texts([query]))[0]

    vector_sql = text("""
        SELECT c.id, c.document_id, c.content, d.title, d.collection,
               1 - (c.embedding <=> :query_emb::vector) AS score
        FROM kb_chunks c
        JOIN kb_documents d ON d.id = c.document_id
        WHERE d.is_published = true
          AND (:collection IS NULL OR d.collection = :collection)
        ORDER BY score DESC
        LIMIT :limit
    """)
    rows_v = await _fetch_rows(db, vector_sql, query_emb=query_embedding, collection=collection, limit=50)

    fts_sql = text("""
        SELECT c.id, c.document_id, c.content, d.title, d.collection,
               ts_rank(to_tsvector('simple', c.content), plainto_tsquery('simple', :query)) AS score
        FROM kb_chunks c
        JOIN kb_documents d ON d.id = c.document_id
        WHERE d.is_published = true
          AND to_tsvector('simple', c.content) @@ plainto_tsquery('simple', :query)
          AND (:collection IS NULL OR d.collection = :collection)
        ORDER BY score DESC
        LIMIT :limit
    """)
    rows_f = await _fetch_rows(db, fts_sql, query=query, collection=collection, limit=50)

    # RRF fusion
    fused = _rrf_fusion(rows_v, rows_f, top_k)

    result_ids = [r.chunk_id for r in fused]
    return fused, result_ids


async def _fetch_rows(db: AsyncSession, sql: text, **params) -> list[dict[str, Any]]:
    result = await db.execute(sql, params)
    rows = result.mappings().all()
    return [dict(r) for r in rows]


def _rrf_fusion(vector_rows: list[dict], fts_rows: list[dict], top_k: int, k: int = 60) -> list[SearchResult]:
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}
    for i, r in enumerate(vector_rows):
        rid = r["id"]
        scores[rid] = scores.get(rid, 0) + 1 / (k + i + 1)
        items[rid] = r
    for i, r in enumerate(fts_rows):
        rid = r["id"]
        scores[rid] = scores.get(rid, 0) + 1 / (k + i + 1)
        if rid not in items:
            items[rid] = r

    ranked = sorted(scores.keys(), key=lambda rid: scores[rid], reverse=True)[:top_k]
    return [
        SearchResult(
            chunk_id=i["id"], document_id=i["document_id"], content=i["content"],
            score=scores[i["id"]], document_title=i["title"], collection=i["collection"],
        )
        for i in (items[rid] for rid in ranked)
    ]


async def generate_answer(query: str, results: list[SearchResult]) -> str:
    """Simple LLM-based answer generator (can be replaced with any provider)."""
    if not results:
        return "검색 결과가 없습니다. 질문을 다시 표현하거나 다른 키워드로 검색해보세요."

    context = "\n\n".join(f"[{r.document_title}]\n{r.content[:500]}" for r in results[:3])
    prompt = f"""You are a helpful support assistant. Answer in Korean using ONLY the context below.
If the context doesn't contain the answer, say you couldn't find relevant information.

Context:
{context}

Question: {query}
Answer:"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.OPENAI_BASE_URL}/chat/completions",
                json={
                    "model": settings.kb_llm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 500,
                },
                headers={"Authorization": f"Bearer {settings.kb_openai_api_key}"},
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning("LLM call failed: %s — returning raw results", e)
        return f"""검색 결과를 찾았습니다. (AI 응답 생성 실패)

{chr(10).join(f'• {r.document_title}: {r.content[:200]}...' for r in results[:3])}"""


async def log_search(db: AsyncSession, query: str, identity: object | None,
                     result_ids: list[str], latency_ms: int) -> str:
    user_id = identity.user.id if identity and hasattr(identity, "user") and identity.user else None
    log = SearchLog(query=query, user_id=user_id, results=result_ids, latency_ms=latency_ms)
    db.add(log)
    await db.commit()
    await db.refresh(log)
    return log.id
