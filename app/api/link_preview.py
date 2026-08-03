"""Link unfurl API  fetches Open Graph metadata for the mobile chat link-preview chip.

GET /api/link-preview?url=...
  returns: { "url": "...", "title": "...", "description": "...", "image": "..." }

Lightweight and unauthenticated, like /api/translate  the preview chip renders
during chat viewing before any account-specific context is available. Rate-limited
per IP since fetching arbitrary URLs is an easy SSRF/DoS/spam vector if left wide
open (see app/services/link_preview_service.py for the SSRF hardening itself).
Results are cached briefly in Redis (when available) so re-rendering the same
message doesn't re-fetch the target URL.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.cache import get as cache_get
from app.cache import set as cache_set
from app.core.logging import get_logger
from app.core.rate_limiter import check_rate_limit, get_client_ip, get_retry_after_seconds
from app.services.link_preview_service import LinkPreviewError, fetch_link_preview

router = APIRouter(tags=["link-preview"])
logger = get_logger(__name__)

_LINK_PREVIEW_LIMIT = dict(max_attempts=30, window_seconds=60)
_CACHE_TTL_SECONDS = 3600  # 1 hour


class LinkPreviewResponse(BaseModel):
    url: str
    title: str | None = None
    description: str | None = None
    image: str | None = None


def _cache_key(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"link_preview:{digest}"


@router.get("/api/link-preview", response_model=LinkPreviewResponse)
async def get_link_preview(
    request: Request,
    url: str = Query(..., min_length=1, max_length=2048, description="URL to unfurl"),
):
    """Fetch og:title / og:description / og:image for `url` (SSRF-hardened server-side fetch)."""
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "link_preview", **_LINK_PREVIEW_LIMIT):
        retry_after = get_retry_after_seconds(client_ip, "link_preview", _LINK_PREVIEW_LIMIT["window_seconds"])
        raise HTTPException(
            status_code=429,
            detail="Too many link preview requests. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    cache_key = _cache_key(url)
    cached = await cache_get(cache_key)
    if cached is not None:
        return LinkPreviewResponse.model_validate_json(cached)

    try:
        preview = await fetch_link_preview(url)
    except LinkPreviewError as exc:
        logger.info("link_preview_rejected", url=url, error=str(exc), status_code=exc.status_code)
        raise HTTPException(status_code=exc.status_code, detail=str(exc))

    response = LinkPreviewResponse(
        url=preview.url,
        title=preview.title,
        description=preview.description,
        image=preview.image,
    )
    await cache_set(cache_key, response.model_dump_json(), ttl=_CACHE_TTL_SECONDS)
    return response
