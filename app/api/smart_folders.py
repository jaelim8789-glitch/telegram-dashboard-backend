"""Smart Folders API — auto-categorize Telegram dialogs by rules."""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_current_identity, Identity
from app.core.logging import get_logger

router = APIRouter(prefix="/api/smart-folders", tags=["smart-folders"])
logger = get_logger(__name__)

_rules: dict[str, list[dict[str, Any]]] = {}

DEFAULT_RULES = [
    {"id": "rf1", "name": "📞 고객문의", "keywords": ["문의", "견적", "가격", "상담"], "color": "#3b82f6", "icon": "💬"},
    {"id": "rf2", "name": "결제/영수증", "keywords": ["결제", "영수증", "입금", "세금", "계산서"], "color": "#22c55e", "icon": "💳"},
    {"id": "rf3", "name": "긴급", "keywords": ["긴급", "장애", "오류", "급함", "확인요망"], "color": "#ef4444", "icon": "🔴"},
    {"id": "rf4", "name": "공지/안내", "keywords": ["공지", "안내", "알림", "업데이트", "점검"], "color": "#a855f7", "icon": "📢"},
    {"id": "rf5", "name": "마케팅", "keywords": ["프로모션", "할인", "이벤트", "쿠폰", "혜택"], "color": "#f97316", "icon": "🎉"},
]


class SmartRule(BaseModel):
    id: str | None = None
    name: str
    keywords: list[str]
    color: str = "#3b82f6"
    icon: str = "📁"


class SmartRuleUpdate(BaseModel):
    name: str | None = None
    keywords: list[str] | None = None
    color: str | None = None
    icon: str | None = None


def _get_key(identity: Identity) -> str:
    return identity.tenant_id or identity.user_id or "default"


@router.get("/rules")
async def list_rules(identity: Identity = Depends(get_current_identity)):
    key = _get_key(identity)
    if key not in _rules:
        _rules[key] = [r.copy() for r in DEFAULT_RULES]
    return _rules[key]


@router.post("/rules")
async def create_rule(rule: SmartRule, identity: Identity = Depends(get_current_identity)):
    key = _get_key(identity)
    if key not in _rules:
        _rules[key] = [r.copy() for r in DEFAULT_RULES]
    new_rule = rule.model_dump()
    new_rule["id"] = f"rule_{datetime.now(timezone.utc).timestamp()}"
    _rules[key].append(new_rule)
    return new_rule


@router.put("/rules/{rule_id}")
async def update_rule(rule_id: str, update: SmartRuleUpdate, identity: Identity = Depends(get_current_identity)):
    key = _get_key(identity)
    rules = _rules.get(key, [])
    for r in rules:
        if r["id"] == rule_id:
            if update.name is not None:
                r["name"] = update.name
            if update.keywords is not None:
                r["keywords"] = update.keywords
            if update.color is not None:
                r["color"] = update.color
            if update.icon is not None:
                r["icon"] = update.icon
            return r
    raise HTTPException(404, "Rule not found")


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str, identity: Identity = Depends(get_current_identity)):
    key = _get_key(identity)
    rules = _rules.get(key, [])
    _rules[key] = [r for r in rules if r["id"] != rule_id]
    return {"status": "deleted"}


@router.post("/categorize")
async def categorize_dialogs(
    dialogs: list[dict],
    identity: Identity = Depends(get_current_identity),
):
    """Categorize a list of dialogs based on smart rules."""
    key = _get_key(identity)
    rules = _rules.get(key, DEFAULT_RULES)

    result: dict[str, list[dict]] = {"기타": []}
    for rule in rules:
        result[rule["name"]] = []

    for dialog in dialogs:
        title = (dialog.get("title") or "").lower()
        last_msg = (dialog.get("last_message") or "").lower()
        search_text = f"{title} {last_msg}"

        categorized = False
        for rule in rules:
            for kw in rule["keywords"]:
                if kw.lower() in search_text:
                    result[rule["name"]].append(dialog)
                    categorized = True
                    break
            if categorized:
                break

        if not categorized:
            result["기타"].append(dialog)

    return result
