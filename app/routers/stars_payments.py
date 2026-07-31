"""
Telegram Stars Payment Router     .

Bot API sendInvoice + pre_checkout_query + successful_payment  
Telegram Stars (currency XTR)    .

 :
1. :     POST /api/stars/create-invoice
2. : sendInvoice(currency="XTR")   Telegram  UI 
3. : pre_checkout_query     answerPreCheckoutQuery
4. : successful_payment     
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

#  Stars  
# 1 Star  100~150 ( Stars   )

STAR_PRODUCTS: dict[str, dict[str, Any]] = {
    "pro_monthly": {
        "title": "Pro 월간 구독",
        "description": "계정 3개 · 월 15,000건 발송 · AI 크레딧 50,000/월",
        "star_amount": 450,
        "plan": Plan.PRO,
        "period_days": 30,
        "label": "Pro",
    },
    "pro_yearly": {
        "title": "Pro 연간 구독 (20% 할인)",
        "description": "계정 3개 · 월 15,000건 발송 · AI 크레딧 50,000/월 · 연간 결제 시 20% 할인",
        "star_amount": 3600,
        "plan": Plan.PRO,
        "period_days": 365,
        "label": "Pro 연간",
    },
    "max_monthly": {
        "title": "Max 월간 구독",
        "description": "계정 10개 · 월 50,000건 발송 · AI 크레딧 200,000/월",
        "star_amount": 1500,
        "plan": Plan.MAX,
        "period_days": 30,
        "label": "Max",
    },
    "ai_boost_1000": {
        "title": "AI Boost 크레딧 1,000",
        "description": "AI 크레딧 1,000개 추가 (플랜과 무관하게 즉시 충전)",
        "star_amount": 300,
        "plan": None,
        "period_days": None,
        "ai_calls": 1000,
        "label": "AI Boost",
    },
    "ai_boost_5000": {
        "title": "AI Boost 크레딧 5,000 (20% 할인)",
        "description": "AI 크레딧 5,000개 추가 (플랜과 무관하게 즉시 충전, 20% 할인)",
        "star_amount": 1200,
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
    """Stars      ( )."""
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
    """ Telegram  Stars Invoice .

    body.product_id   ID
    body.telegram_chat_id  ()  Telegram Chat ID.  bot_sessions .
    """
    product_id = body.get("product_id", "")
    telegram_chat_id_input = body.get("telegram_chat_id")

    product = STAR_PRODUCTS.get(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    client = _get_bot_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram bot not configured")

    # Telegram Chat ID: 1)    2) bot_sessions 
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

    #  invoice payload  (    payload  )
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


#  Helpers 


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
