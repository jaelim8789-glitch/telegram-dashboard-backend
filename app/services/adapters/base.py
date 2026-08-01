"""Base Session Adapter interface for multi-platform support."""

from abc import ABC, abstractmethod
from app.services.state_machine import SessionState


class SessionAdapter(ABC):
    @abstractmethod
    async def connect(self, account_id: str, credentials: dict) -> SessionState:
        ...

    @abstractmethod
    async def disconnect(self, account_id: str) -> None:
        ...

    @abstractmethod
    async def validate(self, account_id: str) -> SessionState:
        ...

    @abstractmethod
    async def get_health(self, account_id: str) -> dict:
        ...

    @property
    @abstractmethod
    def platform(self) -> str:
        ...
