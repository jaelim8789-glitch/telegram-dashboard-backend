import asyncio
import os
import threading
from typing import List

from app.services.ai_memory import AIMemoryProvider


class GraphitiAIMemoryProvider(AIMemoryProvider):
    def __init__(self) -> None:
        self._uri = os.getenv("GRAPHITI_URI", "")
        self._user = os.getenv("GRAPHITI_USER", "")
        self._password = os.getenv("GRAPHITI_PASSWORD", "")
        self._group_id = os.getenv("GRAPHITI_GROUP_ID", "default")
        self._client = None
        self._enabled = False

        if self._uri and self._user and self._password:
            try:
                from graphiti_core import Graphiti

                self._client = Graphiti(
                    uri=self._uri,
                    user=self._user,
                    password=self._password,
                )
                self._enabled = True
            except Exception:
                self._enabled = False

    def _run_async(self, coro):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        else:
            result = None
            exc = None

            def _target():
                nonlocal result, exc
                try:
                    result = asyncio.run(coro)
                except Exception as e:
                    exc = e

            t = threading.Thread(target=_target)
            t.start()
            t.join()
            if exc is not None:
                raise exc
            return result

    async def add_episode(self, user_id: str, episode_body: str, metadata: dict) -> bool:
        if not self._enabled or self._client is None:
            return False
        try:
            from datetime import datetime

            self._run_async(
                self._client.add_episode(
                    name=f"telemon:{user_id}:{datetime.utcnow().isoformat()}",
                    episode_body=episode_body,
                    source_description="TeleMon AI Copilot conversation",
                    reference_time=datetime.utcnow(),
                    source="message",
                    group_id=self._group_id,
                )
            )
            return True
        except Exception:
            return False

    async def search(self, user_id: str, query: str, limit: int = 5) -> List[dict]:
        if not self._enabled or self._client is None:
            return []
        try:
            edges = self._run_async(
                self._client.search(
                    query=query,
                    group_ids=[self._group_id],
                    num_results=limit,
                )
            )
            return [
                {
                    "fact": edge.fact,
                    "group_id": edge.group_id or "unknown",
                    "source": "graphiti",
                }
                for edge in edges
            ]
        except Exception:
            return []
