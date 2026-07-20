"""
Telegram Stars Payment Router — 텔레그램 네이티브 결제 시스템.

Bot API의 sendInvoice + pre_checkout_query + successful_payment 를 사용하여
Telegram Stars (currency XTR) 로 프리미엄 기능을 결제받습니다.

사용 플로우:
1. 프론트: 사용자가 요금제 선택 → POST /api/stars/create-invoice
2. 백엔드: sendInvoice(currency="XTR") 호출 → Telegram이 결제 UI 표시
3. 텔레그램: pre_checkout_query → 백엔드 검증 → answerPreCheckoutQuery
4. 텔레그램: successful_payment → 백엔드에서 사용자 플랜 업그레이드
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.admin_platform import Plan
from app.api.deps import get_current_identity
from app.bot.telegram_api import TelegramBotClient
from app.production_config import get_config

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/stars", tags=["stars-payments"])

# ── Stars 가격표 ───────────────────────────────────────────────────
# 1 Star ≈ ₩100~₩150 (텔레그램 Stars 구매 가격 기준)

STAR_PRODUCTS: dict[str, dict[str, Any]] = {
    "pro_monthly": {
        "title": "Pro 월간 구독",
        "description": "• 10개 계정\n• 일 5,000회 발송\n• AI 분석\n• 우선 지원",
        "star_amount": 1500,        # ≈ ₩15,000
        "plan": Plan.PRO,
        "period_days": 30,
        "label": "Pro",
    },
    "pro_yearly": {
        "title": "Pro 연간 구독 (20% 할인)",
        "description": "• 10개 계정\n• 일 5,000회 발송\n• AI 분석\n• 우선 지원\n• 월 ₩12,000 상당 (20% 할인)",
        "star_amount": 12000,       # ≈ ₩120,000 (월 ₩10,000)
        "plan": Plan.PRO,
        "period_days": 365,
        "label": "Pro 연간",
    },
    "team_monthly": {
        "title": "Team 월간 구독",
        "description": "• 50개 계정\n• 일 50,000회 발송\n• 모든 AI 기능\n• 팀 협업\n• 우선 지원",
        "star_amount": 4500,        # ≈ ₩45,000
        "plan": Plan.TEAM,
        "period_days": 30,
        "label": "Team",
    },
    "ai_boost_1000": {
        "title": "AI Boost — 1,000회 추가",
        "description": "AI 추가 호출 1,000회 (기간 제한 없음)",
        "star_amount": 300,         # ≈ ₩3,000
        "plan": None,               # 플랜 변경 없음
        "period_days": None,
        "ai_calls": 1000,
        "label": "AI Boost",
    },
    "ai_boost_5000": {
        "title": "AI Boost — 5,000회 추가 (20% 할인)",
        "description": "AI 추가 호출 5,000회 (기간 제한 없음, 20% 할인)",
        "star_amount": 1200,        # ≈ ₩12,000
        "plan": None,
        "period_days": None,
        "ai_calls": 5000,
        "label": "AI Boost+",
    },
}


def _get_bot_client() -> TelegramBotClient | None:
    """Get Bot API client for sendInvoice calls."""
    cfg = get_config().telegram_bot
    if not cfg.bot_token:
        return None
    return TelegramBotClient(cfg.bot_token)


@router.get("/products")
async def list_products():
    """Stars 결제 가능한 상품 목록 반환 (인증 불필요)."""
    products = []
    for pid, p in STAR_PRODUCTS.items():
        products.append({
            "id": pid,
            "title": p["title"],
            "description": p["description"],
            "star_amount": p["star_amount"],
            "plan": p.get("plan"),
            "period_days": p.get("period_days"),
            "ai_calls": p.get("ai_calls"),
            "label": p.get("label", ""),
        })
    return {"products": products}


@router.post("/create-invoice")
async def create_invoice(
    body: dict,
    identity=Depends(get_current_identity),
):
    """사용자의 Telegram 계정으로 Stars Invoice 전송.

    body.product_id — 상품 ID
    body.telegram_chat_id — (선택) 사용자의 Telegram Chat ID. 없으면 bot_sessions에서 조회.
    """
    product_id = body.get("product_id", "")
    telegram_chat_id_input = body.get("telegram_chat_id")

    product = STAR_PRODUCTS.get(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    client = _get_bot_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram bot not configured")

    # Telegram Chat ID: 1) 프론트에서 직접 전달 2) bot_sessions에서 조회
    if telegram_chat_id_input:
        try:
            telegram_chat_id = int(telegram_chat_id_input)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="Invalid telegram_chat_id. Must be a numeric chat ID.",
            )
    else:
        telegram_chat_id = await _resolve_telegram_chat(identity)

    if not telegram_chat_id:
        raise HTTPException(
            status_code=400,
            detail="Telegram chat ID not found. Set telegram_chat_id in request body or connect your Telegram account first.",
        )

    # 고유 invoice payload 생성 (결제 완료 시 이 payload로 상품 식별)
    payload_id = str(uuid.uuid4())
    invoice_payload = json.dumps({
        "pid": product_id,
        "uid": identity.user_id if identity else "",
        "iid": payload_id,
    })

    try:
        result = await client.send_invoice(
            chat_id=telegram_chat_id,
            title=product["title"],
            description=product["description"],
            payload=invoice_payload,
            currency="XTR",
            prices=[{
                "label": product.get("label", product_id),
                "amount": product["star_amount"],
            }],
        )

        logger.info(
            "[stars] invoice sent: user=%s product=%s stars=%d",
            identity.user_id if identity else "", product_id, product["star_amount"],
        )

        return {
            "ok": True,
            "invoice_id": result.get("id"),
            "product_id": product_id,
            "star_amount": product["star_amount"],
        }

    except Exception as e:
        logger.error("[stars] sendInvoice failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to create invoice: {e}")


# Webhook is now handled in bot/service.py handle_update()
# Payment updates (pre_checkout_query + successful_payment) arrive
# at the single POST /bot/webhook endpoint alongside regular bot updates.


# ── Helpers ─────────────────────────────────────────────────────────


async def _resolve_telegram_chat(identity) -> int | None:
    from app.bot import db as bot_db
    bot_db.init_bot_tables()
    conn = None
    try:
        import sqlite3
        import os
        db_path = os.environ.get("ADMIN_DB_PATH", "data/admin.db")
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT chat_id FROM bot_sessions ORDER BY updated_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if row:
            return int(row["chat_id"])
        return None
    except Exception:
        return None
    finally:
        if conn:
            conn.close()
