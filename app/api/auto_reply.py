from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import auto_reply as auto_reply_crud
from app.database import get_db
from app.schemas.auto_reply import (
    AutoReplyLogRead,
    AutoReplyRuleCreate,
    AutoReplyRuleRead,
    AutoReplyRuleUpdate,
    AutoReplySettingsRead,
    AutoReplyToggleRequest,
    AutoReplyToggleResponse,
)
from app.services.auto_reply_service import AccountNotAuthenticatedError, disable_auto_reply, enable_auto_reply

router = APIRouter(prefix="/api/accounts/{account_id}/auto-reply", tags=["auto-reply"])
logger = get_logger(__name__)


async def _get_account_or_404(account_id: str, db: AsyncSession):
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    return account


@router.get("", response_model=AutoReplySettingsRead)
async def read_settings(account_id: str, db: AsyncSession = Depends(get_db)):
    account = await _get_account_or_404(account_id, db)
    rules = await auto_reply_crud.list_rules(db, account_id)
    return AutoReplySettingsRead(account_id=account.id, auto_reply_enabled=account.auto_reply_enabled, rules=rules)


@router.post("", response_model=AutoReplyRuleRead, status_code=status.HTTP_201_CREATED)
async def create_rule(account_id: str, payload: AutoReplyRuleCreate, db: AsyncSession = Depends(get_db)):
    await _get_account_or_404(account_id, db)
    rule = await auto_reply_crud.create_rule(db, account_id, payload)
    logger.info("auto_reply_rule_created", account_id=account_id, rule_id=rule.id)
    return rule


@router.put("/{rule_id}", response_model=AutoReplyRuleRead)
async def update_rule(account_id: str, rule_id: str, payload: AutoReplyRuleUpdate, db: AsyncSession = Depends(get_db)):
    await _get_account_or_404(account_id, db)
    rule = await auto_reply_crud.get_rule(db, rule_id)
    if rule is None or rule.account_id != account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="자동 응답 규칙을 찾을 수 없습니다.")
    return await auto_reply_crud.update_rule(db, rule, payload)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(account_id: str, rule_id: str, db: AsyncSession = Depends(get_db)):
    await _get_account_or_404(account_id, db)
    rule = await auto_reply_crud.get_rule(db, rule_id)
    if rule is None or rule.account_id != account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="자동 응답 규칙을 찾을 수 없습니다.")
    await auto_reply_crud.delete_rule(db, rule)
    logger.info("auto_reply_rule_deleted", account_id=account_id, rule_id=rule_id)


@router.post("/toggle", response_model=AutoReplyToggleResponse)
async def toggle(account_id: str, payload: AutoReplyToggleRequest, db: AsyncSession = Depends(get_db)):
    await _get_account_or_404(account_id, db)
    try:
        if payload.enabled:
            await enable_auto_reply(account_id)
        else:
            await disable_auto_reply(account_id)
    except AccountNotAuthenticatedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    logger.info("auto_reply_toggled", account_id=account_id, enabled=payload.enabled)
    return AutoReplyToggleResponse(account_id=account_id, auto_reply_enabled=payload.enabled)


@router.get("/logs", response_model=list[AutoReplyLogRead])
async def read_logs(
    account_id: str,
    rule_id: str | None = None,
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    await _get_account_or_404(account_id, db)
    return await auto_reply_crud.list_logs(db, account_id, rule_id=rule_id, status=status_filter)
