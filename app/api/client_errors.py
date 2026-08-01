"""Client-side error reporting.

The Next.js App Router error boundaries (src/app/**/error.tsx) hide the real
error.message in production, so a beta tester hitting a crash could only ever
screenshot a generic "워크스페이스 오류" message with no way for us to know
what actually threw. This endpoint gives the frontend somewhere to fire-and-forget
report the real error to, so the next occurrence shows up in server logs instead
of requiring a screenshot round-trip.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.core.rate_limiter import check_rate_limit, get_client_ip

logger = get_logger(__name__)
router = APIRouter(prefix="/api/client-errors", tags=["client-errors"])


class ClientErrorReport(BaseModel):
    message: str = Field(..., max_length=2000)
    digest: str | None = Field(None, max_length=100)
    stack: str | None = Field(None, max_length=4000)
    path: str | None = Field(None, max_length=500)
    boundary: str | None = Field(None, max_length=100)


@router.post("", status_code=204)
async def report_client_error(payload: ClientErrorReport, request: Request):
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "client_error_report", max_attempts=20, window_seconds=300):
        return
    logger.error(
        "client_error_boundary_triggered",
        message=payload.message,
        digest=payload.digest,
        path=payload.path,
        boundary=payload.boundary,
        stack=payload.stack,
        client_ip=client_ip,
    )
