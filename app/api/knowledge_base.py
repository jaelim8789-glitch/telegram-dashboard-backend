"""Knowledge Base API — search, ingest, manage, feedback."""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Identity, get_current_identity, get_db
from app.models.knowledge_base import Document, Feedback
from app.schemas.knowledge_base import (DocumentCreate, DocumentOut, FeedbackCreate,
                                         SearchRequest, SearchResponse, SearchResult)
from app.services import knowledge_base as kb

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/kb", tags=["knowledge-base"])


@router.post("/search")
async def search(req: SearchRequest, db: AsyncSession = Depends(get_db),
                 identity: Identity = Depends(get_current_identity)):
    start = time.monotonic()
    results, result_ids = await kb.search_knowledge_base(db, req.query, req.top_k, req.collection)
    answer = await kb.generate_answer(req.query, results)
    latency = int((time.monotonic() - start) * 1000)
    log_id = await kb.log_search(db, req.query, identity, result_ids, latency)
    return SearchResponse(answer=answer, results=results, search_log_id=log_id)


@router.post("/ingest", response_model=DocumentOut)
async def ingest(doc: DocumentCreate, db: AsyncSession = Depends(get_db),
                 identity: Identity = Depends(get_current_identity)):
    if identity.kind not in ("admin", "user"):
        raise HTTPException(403, "관리자 또는 사용자만 문서를 등록할 수 있습니다.")
    result = await kb.ingest_document(db, title=doc.title, content=doc.content, collection=doc.collection,
                                      source_url=doc.source_url, permission_groups=doc.permission_groups,
                                      metadata=doc.metadata, user_id=identity.user.id if identity.user else None)
    return DocumentOut(
        id=result.id, title=result.title, source_url=result.source_url,
        source_type=result.source_type, collection=result.collection,
        permission_groups=result.permission_groups, is_published=result.is_published,
        created_at=result.created_at, updated_at=result.updated_at,
    )


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(
    collection: str | None = None,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    stmt = select(Document).order_by(Document.created_at.desc())
    if collection:
        stmt = stmt.where(Document.collection == collection)
    # 비관리자는 발행된 문서만 조회 가능
    if identity.kind != "admin":
        stmt = stmt.where(Document.is_published == True)  # noqa: E712
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
    if not doc:
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
