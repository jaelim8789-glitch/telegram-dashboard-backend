"""
AI Assistant Platform Backend — TeleMon AI Platform

Modules:
- tools:      AI Tool Calling & MCP Tool 연동
- workflow:   AI Workflow Engine (DAG 기반)
- task_queue: AI Task Queue (Redis/DB 백엔드)
- event_bus:  AI Event Bus (Pub/Sub)
- scheduler:  AI Scheduler (작업 예약)
- plugin:     AI Plugin 구조 (확장 가능)
- api:        AI API 통합 (외부 LLM/서비스)
- models:     SQLAlchemy ORM models
- schemas:    Pydantic schemas
- routers:    FastAPI routers
"""

from __future__ import annotations

from app.ai.config import AiPlatformConfig, get_ai_config

__all__ = [
    "AiPlatformConfig",
    "get_ai_config",
]