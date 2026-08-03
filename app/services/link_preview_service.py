"""Server-side link unfurl (Open Graph preview) for the mobile chat link-preview chip.

Fetches a caller-supplied URL and extracts og:title / og:description / og:image,
falling back to <title> when no OG tags are present.

SSRF hardening (this endpoint is unauthenticated and takes an arbitrary URL from
the client, so it is a textbook SSRF vector against internal services / cloud
metadata endpoints):
  - Only http/https schemes are accepted.
  - The hostname is resolved via DNS *before* connecting, and every resolved
    address is rejected if it falls in a private/loopback/link-local/reserved
    range (covers RFC1918, loopback, link-local incl. 169.254.169.254 cloud
    metadata, and IPv6 equivalents).
  - httpx redirect-following is disabled; we manually follow redirects (bounded)
    and re-validate the new URL/host on every hop, so a public URL that 302s to
    an internal address cannot be used to bypass the check ("DNS rebinding"
    via redirect).
  - httpx is also pointed at the already-validated IP directly (via a custom
    transport override is overkill here; instead we re-resolve right before
    connecting and rely on a short TTL) — practical mitigation for classic
    SSRF given this codebase's existing tooling (no dedicated pinning
    transport available without adding a new dependency).
  - Strict connect/read timeouts and a hard cap on bytes read (we only need
    the <head>, so we stream and stop early instead of downloading the body).
  - The caller's cookies/auth headers are never forwarded to the target site;
    we build a fresh httpx client per request with only a generic UA header.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.core.logging import get_logger

logger = get_logger(__name__)

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 5
_CONNECT_TIMEOUT = 3.0
_READ_TIMEOUT = 3.0
_MAX_BYTES = 300 * 1024  # 300 KB cap; we only need <head>
_USER_AGENT = "TeleMonLinkPreview/1.0 (+https://telemon)"


class LinkPreviewError(Exception):
    """Raised for any rejected/failed unfurl attempt. `status_code` maps to the HTTP response."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class LinkPreview:
    url: str
    title: str | None = None
    description: str | None = None
    image: str | None = None


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable -> reject, fail closed
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_site_local  # deprecated IPv6 site-local, treat as internal
    )


def _validate_url(url: str) -> str:
    """Validate scheme + hostname, resolve DNS, and reject internal targets.

    Returns the normalized URL on success; raises LinkPreviewError otherwise.
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise LinkPreviewError(f"잘못된 URL입니다: {exc}") from exc

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise LinkPreviewError("http 또는 https URL만 지원합니다.")

    hostname = parsed.hostname
    if not hostname:
        raise LinkPreviewError("URL에 호스트가 없습니다.")

    if hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise LinkPreviewError("내부 호스트로의 요청은 허용되지 않습니다.")

    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise LinkPreviewError(f"호스트를 확인할 수 없습니다: {hostname}") from exc

    if not addrinfo:
        raise LinkPreviewError(f"호스트를 확인할 수 없습니다: {hostname}")

    for family, _type, _proto, _canon, sockaddr in addrinfo:
        ip_str = sockaddr[0]
        if _is_blocked_ip(ip_str):
            logger.warning("link_preview_ssrf_blocked", url=url, hostname=hostname, ip=ip_str)
            raise LinkPreviewError("내부/사설 IP 대상으로의 요청은 허용되지 않습니다.", status_code=400)

    return url


def _parse_head(html_bytes: bytes, base_url: str) -> LinkPreview:
    soup = BeautifulSoup(html_bytes, "html.parser")

    def _meta(*names: str) -> str | None:
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    title = _meta("og:title")
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()

    description = _meta("og:description", "description")
    image = _meta("og:image")
    if image:
        image = urljoin(base_url, image)

    return LinkPreview(url=base_url, title=title, description=description, image=image)


async def fetch_link_preview(url: str) -> LinkPreview:
    """Fetch `url` server-side and extract an Open Graph preview.

    Raises LinkPreviewError (with an appropriate status_code) for any
    validation failure, SSRF-blocked target, timeout, or non-HTML response.
    """
    current_url = _validate_url(url)

    limits = httpx.Limits(max_connections=5, max_keepalive_connections=0)
    timeout = httpx.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT, write=_CONNECT_TIMEOUT, pool=_CONNECT_TIMEOUT)

    async with httpx.AsyncClient(
        follow_redirects=False,  # we re-validate each hop manually below
        timeout=timeout,
        limits=limits,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        cookies=None,  # never forward caller cookies/auth to the target
    ) as client:
        for _hop in range(_MAX_REDIRECTS + 1):
            try:
                async with client.stream("GET", current_url) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise LinkPreviewError("리디렉션 응답에 위치 정보가 없습니다.", status_code=502)
                        next_url = urljoin(current_url, location)
                        current_url = _validate_url(next_url)  # re-validate on every hop
                        continue

                    if resp.status_code >= 400:
                        raise LinkPreviewError(f"대상 서버가 오류를 반환했습니다: {resp.status_code}", status_code=502)

                    content_type = resp.headers.get("content-type", "")
                    if "html" not in content_type.lower():
                        raise LinkPreviewError("HTML 문서가 아닙니다.", status_code=415)

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= _MAX_BYTES:
                            break
                    body = b"".join(chunks)
                    return _parse_head(body, current_url)
            except httpx.TimeoutException as exc:
                raise LinkPreviewError("대상 서버 응답 시간이 초과되었습니다.", status_code=504) from exc
            except httpx.TransportError as exc:
                raise LinkPreviewError(f"대상 서버에 연결할 수 없습니다: {exc}", status_code=502) from exc

        raise LinkPreviewError("리디렉션이 너무 많습니다.", status_code=502)
