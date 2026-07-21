"""Message preview, validation, and template variable resolution."""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity
from app.core.logging import get_logger
from app.database import get_db
from app.crud import message_template as template_crud

router = APIRouter(tags=["preview"])
logger = get_logger(__name__)

VARIABLE_PATTERN = re.compile(r"\{\{(\w+)\}\}")


def extract_variables(text: str) -> list[str]:
    return sorted(set(VARIABLE_PATTERN.findall(text)))


def resolve_template(text: str, variables: dict[str, str]) -> str:
    def _replace(m: re.Match) -> str:
        key = m.group(1)
        return variables.get(key, m.group(0))
    return VARIABLE_PATTERN.sub(_replace, text)


@router.post("/api/preview/resolve")
async def resolve_message(
    message: Annotated[str, Form()],
    variables_json: Annotated[str, Form(description='JSON object of variable values, e.g. {"name":"홍길동","date":"7월 17일"}')],
    identity: Identity = Depends(get_current_identity),
):
    import json
    try:
        variables = json.loads(variables_json)
        if not isinstance(variables, dict):
            raise ValueError("Must be a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"Invalid variables JSON: {e}")

    detected = extract_variables(message)
    missing = [v for v in detected if v not in variables]
    resolved = resolve_template(message, variables)

    return {
        "original": message,
        "resolved": resolved,
        "variables_detected": detected,
        "variables_provided": list(variables.keys()),
        "variables_missing": missing,
        "has_unresolved": len(missing) > 0,
        "char_count": len(resolved),
    }


@router.post("/api/preview/validate")
async def validate_message(
    message: Annotated[str, Form(max_length=4096)],
    identity: Identity = Depends(get_current_identity),
):
    issues = []

    if not message or not message.strip():
        issues.append({"type": "error", "field": "message", "message": "메시지 내용이 비어 있습니다."})

    variables = extract_variables(message)
    for var in variables:
        issues.append({"type": "warning", "field": "variable", "message": f"변수 '{{{var}}}'가 정의되지 않았습니다. resolve 엔드포인트를 사용하세요."})

    if len(message) > 4000:
        issues.append({"type": "warning", "field": "length", "message": f"메시지가 4000자를 초과합니다 ({len(message)}자). Telegram이 메시지를 자를 수 있습니다."})

    entity_count = len(message) // 100 + 1
    if entity_count > 100:
        issues.append({"type": "warning", "field": "entities", "message": "메시지에 포함된 엔티티가 많아 일부 서식이 무시될 수 있습니다."})

    return {
        "message": message,
        "char_count": len(message),
        "variable_count": len(variables),
        "variables": variables,
        "issues": issues,
        "is_valid": all(i["type"] == "warning" for i in issues),
        "estimated_segments": max(1, len(message) // 4096 + 1),
    }


@router.get("/api/templates/{template_id}/preview")
async def preview_template(
    template_id: str,
    variables_json: str = Query("{}", alias="variables", description='JSON object, e.g. {"name":"홍길동"}'),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    import json
    try:
        variables = json.loads(variables_json)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="Invalid JSON in variables parameter")

    template = await template_crud.get_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")

    if identity.kind != "admin" and template.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="다른 테넌트의 템플릿을 미리볼 수 없습니다.")

    content = template.content
    detected = extract_variables(content)
    missing = [v for v in detected if v not in variables]
    resolved = resolve_template(content, variables)

    return {
        "template_id": template.id,
        "template_name": template.name,
        "original": content,
        "resolved": resolved,
        "variables_detected": detected,
        "variables_missing": missing,
        "char_count": len(resolved),
    }
