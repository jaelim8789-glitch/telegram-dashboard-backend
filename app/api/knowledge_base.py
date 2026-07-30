"""Knowledge Base API — search, ingest, manage, feedback."""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Identity, get_current_identity, get_db
from app.models.knowledge_base import Document, Feedback, SearchLog
from app.schemas.knowledge_base import (DocumentCreate, DocumentOut, FeedbackCreate,
                                         SearchRequest, SearchResponse, SearchResult)
from app.services import knowledge_base as kb

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/kb", tags=["knowledge-base"])


@router.post("/search")
async def search(req: SearchRequest, db: AsyncSession = Depends(get_db),
                 identity: Identity = Depends(get_current_identity)):
    start = time.monotonic()
    results, result_ids = await kb.search_knowledge_base(db, identity.tenant_id, req.query, req.top_k, req.collection)
    answer = await kb.generate_answer(req.query, results)
    latency = int((time.monotonic() - start) * 1000)
    log_id = await kb.log_search(db, identity.tenant_id, req.query, identity, result_ids, latency)
    return SearchResponse(answer=answer, results=results, search_log_id=log_id)


@router.post("/ingest", response_model=DocumentOut)
async def ingest(doc: DocumentCreate, db: AsyncSession = Depends(get_db),
                 identity: Identity = Depends(get_current_identity)):
    if identity.kind not in ("admin", "user"):
        raise HTTPException(403, "관리자 또는 사용자만 문서를 등록할 수 있습니다.")
    result = await kb.ingest_document(db, tenant_id=identity.tenant_id, title=doc.title, content=doc.content,
                                      collection=doc.collection, source_url=doc.source_url,
                                      permission_groups=doc.permission_groups,
                                      metadata=doc.metadata, user_id=identity.user.id if identity.user else None)
    return DocumentOut(
        id=result.id, title=result.title, source_url=result.source_url,
        source_type=result.source_type, collection=result.collection,
        permission_groups=result.permission_groups, is_published=result.is_published,
        created_at=result.created_at, updated_at=result.updated_at,
    )


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(collection: str | None = None, db: AsyncSession = Depends(get_db),
                         identity: Identity = Depends(get_current_identity)):
    stmt = select(Document).where(Document.tenant_id == identity.tenant_id).order_by(Document.created_at.desc())
    if collection:
        stmt = stmt.where(Document.collection == collection)
    result = await db.execute(stmt)
    docs = result.scalars().all()
    return [DocumentOut(
        id=d.id, title=d.title, source_url=d.source_url, source_type=d.source_type,
        collection=d.collection, permission_groups=d.permission_groups,
        is_published=d.is_published, created_at=d.created_at, updated_at=d.updated_at,
    ) for d in docs]


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_document(doc_id: str, db: AsyncSession = Depends(get_db),
                          identity: Identity = Depends(get_current_identity)):
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != identity.tenant_id:
        raise HTTPException(404, "Document not found")
    await db.delete(doc)
    await db.commit()


@router.post("/feedback")
async def submit_feedback(fb: FeedbackCreate, db: AsyncSession = Depends(get_db),
                          identity: Identity = Depends(get_current_identity)):
    entry = Feedback(search_log_id=fb.search_log_id,
                     user_id=identity.user.id if identity.user else None,
                     rating=fb.rating, comment=fb.comment)
    db.add(entry)
    await db.commit()
    return {"status": "ok"}


@router.get("/admin/stats")
async def kb_stats(db: AsyncSession = Depends(get_db),
                   identity: Identity = Depends(get_current_identity)):
    if identity.kind not in ("admin",):
        raise HTTPException(403, "관리자만 조회할 수 있습니다.")

    total_docs = await db.scalar(select(func.count(Document.id)))
    total_searches = await db.scalar(select(func.count(SearchLog.id)))
    avg_latency = await db.scalar(select(func.avg(SearchLog.latency_ms)))
    recent_searches = await db.execute(
        select(SearchLog.query, func.count(SearchLog.id).label("cnt"))
        .where(SearchLog.created_at >= func.now() - text("interval '7 days'"))
        .group_by(SearchLog.query)
        .order_by(text("cnt DESC"))
        .limit(10)
    )
    top_queries = [{"query": r[0], "count": r[1]} for r in recent_searches.all()]

    return {
        "total_documents": total_docs or 0,
        "total_searches": total_searches or 0,
        "avg_latency_ms": round(avg_latency or 0, 1),
        "top_queries_last_7d": top_queries,
        "collections": list(dict.fromkeys(r[0] for r in await db.execute(
            select(Document.collection).distinct()
        ).all())),
    }
