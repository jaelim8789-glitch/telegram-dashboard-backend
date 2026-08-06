"""Web Search (Tavily) — P6.

Searches the web for current/up-to-date info when local KB/memory/history
don't cover the question. No Tavily key configured → gracefully returns []
(never crashes the chat pipeline).

LLM-independent: swapping the model keeps this engine intact.
"""

from __future__ import annotations

import httpx

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

TAVILY_ENDPOINT = "https://api.tavily.com/search"


async def web_search(query: str, max_results: int = 4) -> list[dict]:
    """Search the web and return a list of {title, url, content} results.

    Returns [] when Tavily isn't configured or the call fails.
    """
    api_key = settings.tavily_api_key
    if not api_key:
        return []

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                TAVILY_ENDPOINT,
                json={
                    "api_key": api_key,
                    "query": query[:400],
                    "max_results": max_results,
                    "search_depth": "basic",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            results = []
            for r in data.get("results", []):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", ""),
                })
            return results
    except Exception as exc:
        logger.debug("web_search_failed", error=str(exc))
        return []
