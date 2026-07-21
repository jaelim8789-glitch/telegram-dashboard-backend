"""
AI Function Calling Tools — TeleMon 운영 API를 AI Agent가 호출할 수 있도록 정의합니다.

Tool 분류:
- read (조회): 즉시 실행, 결과를 AI 응답에 포함
- write (실행): 사용자 확인 후 실행, POST /api/ai/chats/{chat_id}/confirm-tool 로 승인
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── Tool definitions (OpenAI-compatible function calling schema) ───────────

TOOLS = [
    # ── Read tools (즉시 실행) ──────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_delivery_summary",
            "description": "최근 발송 현황 요약을 조회합니다. 성공률, 총 발송 수, 실패 수 등을 반환합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "조회할 기간(일), 기본값 7",
                        "default": 7,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_delivery_overview",
            "description": "발송 분석 종합 개요를 조회합니다. 요약, 소스별 분석, 계정별 성과, 실패 분석, 타임라인을 포함합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "조회할 기간(일), 기본값 7",
                        "default": 7,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_failure_breakdown",
            "description": "발송 실패 내역 분석을 조회합니다. 실패 유형별 통계를 반환합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "조회할 기간(일), 기본값 7",
                        "default": 7,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_account_performance",
            "description": "계정별 발송 성과를 조회합니다. 각 계정의 성공률, 발송 수 등을 반환합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "조회할 기간(일), 기본값 7",
                        "default": 7,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_activity",
            "description": "최근 발송 활동 내역을 조회합니다. 최신 발송 로그를 반환합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "조회할 항목 수, 기본값 20",
                        "default": 20,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_account_list",
            "description": "등록된 텔레그램 계정 목록을 조회합니다.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_group_list",
            "description": "등록된 그룹/채널 목록을 조회합니다.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_source_analytics",
            "description": "발송 소스별(브로드캐스트, 매크로, 수동 등) 분석을 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "조회할 기간(일), 기본값 7",
                        "default": 7,
                    },
                },
            },
        },
    },
    # ── Write tools (사용자 확인 필요) ──────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "send_broadcast",
            "description": "지정한 수신자들에게 메시지를 발송합니다. 실행 전에 반드시 사용자 확인이 필요합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {
                        "type": "string",
                        "description": "발송에 사용할 계정 ID",
                    },
                    "message": {
                        "type": "string",
                        "description": "발송할 메시지 내용",
                    },
                    "recipients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "수신자 목록 (그룹 ID 또는 사용자 ID)",
                    },
                    "group_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "발송할 그룹 ID 목록 (recipients 대신 사용 가능)",
                    },
                },
                "required": ["account_id", "message"],
            },
        },
    },
]

# Tool metadata: risk level, requires confirmation, description
TOOL_META: dict[str, dict[str, Any]] = {
    "get_delivery_summary": {"category": "read", "requires_confirmation": False, "label": "📊 발송 현황 조회"},
    "get_delivery_overview": {"category": "read", "requires_confirmation": False, "label": "📋 발송 종합 분석"},
    "get_failure_breakdown": {"category": "read", "requires_confirmation": False, "label": "⚠️ 실패 분석"},
    "get_account_performance": {"category": "read", "requires_confirmation": False, "label": "📱 계정 성과"},
    "get_recent_activity": {"category": "read", "requires_confirmation": False, "label": "🕐 최근 활동"},
    "get_account_list": {"category": "read", "requires_confirmation": False, "label": "📱 계정 목록"},
    "get_group_list": {"category": "read", "requires_confirmation": False, "label": "👥 그룹 목록"},
    "get_source_analytics": {"category": "read", "requires_confirmation": False, "label": "📊 소스별 분석"},
    "send_broadcast": {"category": "write", "requires_confirmation": True, "label": "📨 메시지 발송"},
}


@dataclass
class ToolResult:
    success: bool
    tool_name: str
    result: Any
    error: str | None = None


# ── Tool executors ────────────────────────────────────────────────────────

async def execute_tool(
    tool_name: str,
    arguments: dict,
    identity: Any,  # Identity from app.api.deps
) -> ToolResult:
    """Execute a tool by name with given arguments.

    Read tools are executed immediately with the identity context.
    Write tools return a pending result — actual execution happens via
    confirm-tool endpoint after user approval.
    """
    from app.services.delivery_analytics import (
        get_summary,
        get_overview,
        get_failure_breakdown,
        get_account_performance,
        get_recent_activity,
        get_source_analytics,
    )

    try:
        if tool_name == "get_delivery_summary":
            days = arguments.get("days", 7)
            result = await get_summary(identity, days=days)
            return ToolResult(success=True, tool_name=tool_name, result=result)

        elif tool_name == "get_delivery_overview":
            days = arguments.get("days", 7)
            result = await get_overview(identity, days=days)
            return ToolResult(success=True, tool_name=tool_name, result=result)

        elif tool_name == "get_failure_breakdown":
            days = arguments.get("days", 7)
            result = await get_failure_breakdown(identity, days=days)
            return ToolResult(success=True, tool_name=tool_name, result=result)

        elif tool_name == "get_account_performance":
            days = arguments.get("days", 7)
            result = await get_account_performance(identity, days=days)
            return ToolResult(success=True, tool_name=tool_name, result=result)

        elif tool_name == "get_recent_activity":
            limit = arguments.get("limit", 20)
            result = await get_recent_activity(identity, limit=limit)
            return ToolResult(success=True, tool_name=tool_name, result=result)

        elif tool_name == "get_source_analytics":
            days = arguments.get("days", 7)
            result = await get_source_analytics(identity, days=days)
            return ToolResult(success=True, tool_name=tool_name, result=result)

        elif tool_name == "get_account_list":
            from app.crud.account import get_accounts
            accounts = await get_accounts(identity)
            return ToolResult(success=True, tool_name=tool_name, result={"accounts": accounts})

        elif tool_name == "get_group_list":
            from app.api.groups import _get_all_groups_for_tenant
            groups = await _get_all_groups_for_tenant(identity)
            return ToolResult(success=True, tool_name=tool_name, result={"groups": groups})

        elif tool_name == "send_broadcast":
            # Write tool — not executed here, will be confirmed separately
            return ToolResult(
                success=True,
                tool_name=tool_name,
                result={"pending": True, "arguments": arguments},
            )

        else:
            return ToolResult(
                success=False,
                tool_name=tool_name,
                result=None,
                error=f"Unknown tool: {tool_name}",
            )

    except Exception as exc:
        return ToolResult(
            success=False,
            tool_name=tool_name,
            result=None,
            error=str(exc),
        )