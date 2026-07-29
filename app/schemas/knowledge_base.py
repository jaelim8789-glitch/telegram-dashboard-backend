"""Pydantic schemas for Knowledge Base."""

from datetime import datetime

from pydantic import BaseModel, Field


class DocumentCreate(BaseModel):
    title: str = Field(..., max_length=500)
    content: str
    source_url: str | None = None
    collection: str = "general"
    permission_groups: list[str] = []
    metadata: dict = {}


class DocumentOut(BaseModel):
    id: str
    title: str
    source_url: str | None
    source_type: str
    collection: str
    permission_groups: list[str]
    is_published: bool
    created_at: datetime
    updated_at: datetime


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)
    collection: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)


class SearchResult(BaseModel):
    chunk_id: str
    document_id: str
    content: str
    score: float
    document_title: str
    collection: str


class SearchResponse(BaseModel):
    answer: str
    results: list[SearchResult]
    search_log_id: str


class FeedbackCreate(BaseModel):
    search_log_id: str
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = None
