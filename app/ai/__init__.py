"""
AI Assistant Platform Backend  TeleMon AI Platform

Modules:
- tools:      AI Tool Calling & MCP Tool 
- workflow:   AI Workflow Engine (DAG )
- task_queue: AI Task Queue (Redis/DB )
- event_bus:  AI Event Bus (Pub/Sub)
- scheduler:  AI Scheduler ( )
- plugin:     AI Plugin  ( )
- api:        AI API  ( LLM/)
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