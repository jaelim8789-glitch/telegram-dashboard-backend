import json
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity
from app.core.logging import get_logger
from app.database import get_db
from app.models.employee import Employee, EmployeeMessage
from app.services.ai_core_service import call_deepseek

router = APIRouter(prefix="/api/employees", tags=["employees"])
logger = get_logger(__name__)

EXP_PER_MSG = 10
EXP_PER_LEVEL = 100

ROLE_PROMPTS = {
    "assistant": "당신은 사용자의 개인 AI 비서입니다. 친절하고 전문적으로 답변해주세요.",
    "marketer": "당신은 마케팅 전문가입니다. 효과적인 마케팅 전략과 카피를 제안해주세요.",
    "searcher": "당신은 웹 검색 및 정보 분석 전문가입니다. 정확하고 신뢰성 있는 정보를 제공해주세요.",
    "supporter": "당신은 고객 지원 전문가입니다. 친절하고 정확하게 고객 문의에 응대해주세요.",
}


def _build_system_prompt(emp: Employee) -> str:
    base = ROLE_PROMPTS.get(emp.role, ROLE_PROMPTS["assistant"])
    extras = []
    if emp.personality:
        extras.append(f"성격: {emp.personality}")
    if emp.expertise:
        extras.append(f"전문 분야: {emp.expertise}")
    if emp.system_prompt:
        extras.append(emp.system_prompt)
    if extras:
        base += "\n\n" + "\n".join(extras)
    base += f"\n\n당신의 레벨은 {emp.level}이며, 총 {emp.total_messages}개의 메시지를 처리했습니다."
    return base


# ─── CRUD ────────────────────────────────────────────────────────────────

@router.get("")
async def list_employees(
    market: bool = Query(False, description="마켓플레이스 목록 조회"),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """내 직원 목록 조회 (market=true 시 마켓플레이스 전체 목록)."""
    if market:
        query = select(Employee).where(Employee.is_listed.is_(True)).order_by(Employee.level.desc())
    else:
        query = select(Employee).where(Employee.tenant_id == identity.tenant_id).order_by(Employee.updated_at.desc())
    result = await db.execute(query)
    emps = result.scalars().all()
    return [
        {
            "id": e.id,
            "name": e.name,
            "role": e.role,
            "level": e.level,
            "exp": e.exp,
            "total_messages": e.total_messages,
            "personality": e.personality,
            "expertise": e.expertise,
            "avatar": e.avatar,
            "is_listed": e.is_listed,
            "price": e.price,
            "rental_price": e.rental_price,
            "created_at": e.created_at.isoformat(),
        }
        for e in emps
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_employee(
    body: dict,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """새 AI 직원 생성."""
    emp = Employee(
        tenant_id=identity.tenant_id,
        name=body.get("name", "새 직원"),
        role=body.get("role", "assistant"),
        personality=body.get("personality", ""),
        expertise=body.get("expertise", ""),
        system_prompt=body.get("system_prompt", ""),
    )
    db.add(emp)
    await db.commit()
    await db.refresh(emp)
    logger.info("employee_created", employee_id=emp.id, tenant_id=identity.tenant_id)
    return {"id": emp.id, "name": emp.name, "role": emp.role}


@router.get("/{employee_id}")
async def get_employee(
    employee_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """직원 상세 조회."""
    emp = await db.get(Employee, employee_id)
    if emp is None:
        raise HTTPException(status_code=404, detail="직원을 찾을 수 없습니다.")
    return {
        "id": emp.id,
        "name": emp.name,
        "role": emp.role,
        "level": emp.level,
        "exp": emp.exp,
        "total_messages": emp.total_messages,
        "personality": emp.personality,
        "expertise": emp.expertise,
        "system_prompt": emp.system_prompt,
        "avatar": emp.avatar,
        "is_listed": emp.is_listed,
        "price": emp.price,
        "rental_price": emp.rental_price,
    }


@router.patch("/{employee_id}")
async def update_employee(
    employee_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """직원 정보 수정."""
    emp = await db.get(Employee, employee_id)
    if emp is None or emp.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="직원을 찾을 수 없습니다.")
    for field in ("name", "role", "personality", "expertise", "system_prompt", "avatar", "is_listed", "price", "rental_price"):
        if field in body:
            setattr(emp, field, body[field])
    await db.commit()
    return {"status": "updated"}


@router.delete("/{employee_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_employee(
    employee_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """직원 삭제."""
    emp = await db.get(Employee, employee_id)
    if emp is None or emp.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="직원을 찾을 수 없습니다.")
    await db.delete(emp)
    await db.commit()
    logger.info("employee_deleted", employee_id=employee_id)


# ─── 채팅 ────────────────────────────────────────────────────────────────

@router.get("/{employee_id}/messages")
async def get_employee_messages(
    employee_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """직원과의 대화 내역 조회."""
    result = await db.execute(
        select(EmployeeMessage)
        .where(EmployeeMessage.employee_id == employee_id)
        .order_by(EmployeeMessage.created_at)
    )
    msgs = result.scalars().all()
    return [{"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at.isoformat()} for m in msgs]


@router.post("/{employee_id}/chat")
async def chat_with_employee(
    employee_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """AI 직원과 대화."""
    emp = await db.get(Employee, employee_id)
    if emp is None:
        raise HTTPException(status_code=404, detail="직원을 찾을 수 없습니다.")

    question = body.get("question", "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="질문을 입력하세요")

    # 사용자 메시지 저장
    db.add(EmployeeMessage(employee_id=employee_id, tenant_id=identity.tenant_id, role="user", content=question))

    # 과거 메시지 불러오기
    result = await db.execute(
        select(EmployeeMessage)
        .where(EmployeeMessage.employee_id == employee_id)
        .order_by(EmployeeMessage.created_at)
        .limit(50)
    )
    history = list(result.scalars().all())

    # 시스템 프롬프트 + 히스토리 구성
    system_prompt = _build_system_prompt(emp)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend({"role": m.role, "content": m.content} for m in history)

    # AI 호출
    answer, tokens = await call_deepseek(messages, max_tokens=2000)

    if not answer:
        return {"answer": "죄송합니다. 응답 생성에 실패했습니다.", "tokens": 0, "exp_gained": 0, "level_up": False}

    # AI 응답 저장
    db.add(EmployeeMessage(employee_id=employee_id, tenant_id=identity.tenant_id, role="assistant", content=answer, tokens_used=tokens))

    # 경험치 & 레벨
    old_level = emp.level
    emp.total_messages += 1
    emp.exp += EXP_PER_MSG + (tokens // 100)
    while emp.exp >= EXP_PER_LEVEL * emp.level:
        emp.exp -= EXP_PER_LEVEL * emp.level
        emp.level += 1

    await db.commit()

    return {
        "answer": answer,
        "tokens": tokens,
        "exp_gained": EXP_PER_MSG + (tokens // 100),
        "level_up": emp.level > old_level,
        "new_level": emp.level,
        "total_messages": emp.total_messages,
    }


# ─── 구매/임대 (마켓플레이스) ──────────────────────────────────────────────

@router.post("/{employee_id}/purchase")
async def purchase_employee(
    employee_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """마켓플레이스에서 직원 구매."""
    emp = await db.get(Employee, employee_id)
    if emp is None or not emp.is_listed:
        raise HTTPException(status_code=404, detail="구매할 수 없는 직원입니다.")

    # 원본 복제
    new_emp = Employee(
        tenant_id=identity.tenant_id,
        name=emp.name,
        role=emp.role,
        personality=emp.personality,
        expertise=emp.expertise,
        system_prompt=emp.system_prompt,
        avatar=emp.avatar,
        original_owner_id=emp.original_owner_id or emp.tenant_id,
    )
    db.add(new_emp)
    await db.commit()
    return {"id": new_emp.id, "name": new_emp.name}