import os
from abc import ABC, abstractmethod
from typing import List


class AIMemoryProvider(ABC):
    @abstractmethod
    async def add_episode(self, user_id: str, episode_body: str, metadata: dict) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def search(self, user_id: str, query: str, limit: int = 5) -> List[dict]:
        raise NotImplementedError


class NoOpAIMemoryProvider(AIMemoryProvider):
    async def add_episode(self, user_id: str, episode_body: str, metadata: dict) -> bool:
        return False

    async def search(self, user_id: str, query: str, limit: int = 5) -> List[dict]:
        return []


def get_ai_memory_provider() -> AIMemoryProvider:
    from app.config import settings

    if not settings.graphiti_enabled:
        return NoOpAIMemoryProvider()
    try:
        from app.services.graphiti_ai_memory import GraphitiAIMemoryProvider

        return GraphitiAIMemoryProvider()
    except Exception:
        return NoOpAIMemoryProvider()
