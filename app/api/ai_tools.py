"""
AI Function Calling Tools  TeleMon  API AI Agent    .

Tool :
- read ():  ,  AI  
- write ():    , POST /api/ai/chats/{chat_id}/confirm-tool  
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#  Tool definitions (OpenAI-compatible function calling schema) 

TOOLS = [
    #  Read tools ( ) 
    {
        "type": "function",
        "function": {
            "name": "get_delivery_summary",
            "description": "    . ,   ,    .",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": " (),  7",
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
            "description": "    . ,  ,  ,  ,  .",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": " (),  7",
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
            "description": "    .    .",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": " (),  7",
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
            "description": "   .   ,    .",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": " (),  7",
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
            "description": "    .    .",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "  ,  20",
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
            "description": "    .",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_group_list",
            "description": " /  .",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_source_analytics",
            "description": " (, ,  )  .",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": " (),  7",
                        "default": 7,
                    },
                },
            },
        },
    },
    #  Write tools (  ) 
    {
        "type": "function",
        "function": {
            "name": "send_broadcast",
            "description": "   .      .",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {
                        "type": "string",
                        "description": "   ID",
                    },
                    "message": {
                        "type": "string",
                        "description": "  ",
                    },
                    "recipients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "  ( ID   ID)",
                    },
                    "group_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "  ID  (recipients   )",
                    },
                },
                "required": ["account_id", "message"],
            },
        },
    },
    #  Conversation tools (AI = personal assistant) 
    {
        "type": "function",
        "function": {
            "name": "get_chat_messages",
            "description": "텔레그램 특정 채팅/그룹의 최근 대화 메시지를 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string", "description": "계정 ID"},
                    "chat_id": {"type": "integer", "description": "채팅/그룹 ID"},
                    "limit": {"type": "integer", "description": "조회할 메시지 수 (기본 20)", "default": 20},
                },
                "required": ["account_id", "chat_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_chat_reply",
            "description": "텔레그램 특정 채팅/그룹에 메시지를 보냅니다. 사용자 확인 후 실행됩니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string", "description": "계정 ID"},
                    "chat_id": {"type": "integer", "description": "채팅/그룹 ID"},
                    "message": {"type": "string", "description": "보낼 메시지"},
                },
                "required": ["account_id", "chat_id", "message"],
            },
        },
    },
]

# Tool metadata: risk level, requires confirmation, description
TOOL_META: dict[str, dict[str, Any]] = {
    "get_delivery_summary": {"category": "read", "requires_confirmation": False, "label": "   "},
    "get_delivery_overview": {"category": "read", "requires_confirmation": False, "label": "   "},
    "get_failure_breakdown": {"category": "read", "requires_confirmation": False, "label": "  "},
    "get_account_performance": {"category": "read", "requires_confirmation": False, "label": "  "},
    "get_recent_activity": {"category": "read", "requires_confirmation": False, "label": "  "},
    "get_account_list": {"category": "read", "requires_confirmation": False, "label": "  "},
    "get_group_list": {"category": "read", "requires_confirmation": False, "label": "  "},
    "get_source_analytics": {"category": "read", "requires_confirmation": False, "label": "  "},
    "get_chat_messages": {"category": "read", "requires_confirmation": False, "label": "대화 조회"},
    "send_chat_reply": {"category": "write", "requires_confirmation": True, "label": "답장 보내기"},
    "send_broadcast": {"category": "write", "requires_confirmation": True, "label": "  "},
}


@dataclass
class ToolResult:
    success: bool
    tool_name: str
    result: Any
    error: str | None = None


#  Tool executors 

async def execute_tool(
    tool_name: str,
    arguments: dict,
    identity: Any,  # Identity from app.api.deps
) -> ToolResult:
    """Execute a tool by name with given arguments.

    Read tools are executed immediately with the identity context.
    Write tools return a pending result  actual execution happens via
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

        elif tool_name == "get_chat_messages":
            from app.services.chat_actions import fetch_messages
            account_id = arguments.get("account_id")
            chat_id = arguments.get("chat_id")
            limit = arguments.get("limit", 20)
            if not account_id or not chat_id:
                return ToolResult(success=False, tool_name=tool_name, result=None, error="account_id와 chat_id가 필요합니다.")
            msgs = await fetch_messages(account_id, int(chat_id), limit=int(limit))
            return ToolResult(success=True, tool_name=tool_name, result={"messages": msgs[: int(limit)]})

        elif tool_name == "send_chat_reply":
            from app.services.chat_actions import send_chat_message
            account_id = arguments.get("account_id")
            chat_id = arguments.get("chat_id")
            message = arguments.get("message", "")
            if not account_id or not chat_id or not message:
                return ToolResult(success=False, tool_name=tool_name, result=None, error="account_id, chat_id, message가 필요합니다.")
            sent = await send_chat_message(account_id, int(chat_id), message)
            return ToolResult(success=True, tool_name=tool_name, result={"sent": sent})

        elif tool_name == "send_broadcast":
            # Write tool  requires prior user confirmation via the
            # /confirm-tool endpoint. That endpoint calls execute_tool()
            # again after confirmation, at which point we actually send.
            from app.services.bot_ai_agent_service import _execute_send_broadcast

            result = await _execute_send_broadcast(arguments)
            return ToolResult(success=True, tool_name=tool_name, result=result)

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