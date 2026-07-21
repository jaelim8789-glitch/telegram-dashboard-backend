"""
NOWPayments Service — 암호화폐 결제 처리를 위한 서비스.

API 문서 기반으로 구현:
- Invoice 생성
- IPN (Instant Payment Notification) 서명 검증
- 금액 검증
- 중복 방지 로직
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.core.plans import PLAN_CATALOG, get_plan
from app.models.nowpayments import NowPaymentsTransaction
from app.models.tenant import Tenant
from app.services.cryptomus import activate_tenant_plan

logger = get_logger(__name__)


class NOWPaymentsService:
    def __init__(self):
        self.api_key = settings.NOWPAYMENTS_API_KEY
        self.public_key = settings.NOWPAYMENTS_PUBLIC_KEY
        self.ipn_secret = settings.NOWPAYMENTS_IPN_SECRET
        self.base_url = "https://api.nowpayments.io/v1"
        
    async def create_payment(self, amount: float, currency: str, plan_id: str, tenant_id: str, order_description: str = "TeleMon Subscription"):
        """
        NOWPayments를 통해 결제 인보이스 생성
        
        Args:
            amount: 결제 금액
            currency: 통화 (예: usdt, btc, eth 등)
            plan_id: 플랜 ID
            tenant_id: 테넌트 ID
            order_description: 주문 설명
            
        Returns:
            생성된 결제 정보
        """
        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json"
        }
        
        payload = {
            "price_amount": amount,
            "price_currency": "usd",  # 항상 USD로 고정
            "pay_currency": currency.lower(),
            "order_id": f"tenant_{tenant_id}_plan_{plan_id}_{int(datetime.now().timestamp())}",
            "order_description": order_description,
            "ipn_callback_url": f"{settings.base_url}/api/payments/nowpayments/webhook",
            "success_redirect_url": f"{settings.frontend_url}/payment/success",
            "cancel_redirect_url": f"{settings.frontend_url}/payment/cancel"
        }
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.base_url}/payment",
                    headers=headers,
                    json=payload
                )
                
                if response.status_code != 200:
                    logger.error(f"NOWPayments API error: {response.status_code} - {response.text}")
                    raise RuntimeError(f"NOWPayments API error: {response.status_code}")
                    
                result = response.json()
                
                # 데이터베이스에 거래 정보 저장
                transaction = NowPaymentsTransaction(
                    id=result['payment_id'],  # Using payment_id as the primary key id
                    payment_id=result['payment_id'],
                    tenant_id=tenant_id,
                    plan_id=plan_id,
                    amount=result['price_amount'],
                    pay_currency=result['pay_currency'],
                    order_id=result['order_id'],
                    payment_status='created',
                    created_at=datetime.utcnow()
                )
                
                return result
        except Exception as e:
            logger.error(f"Error creating NOWPayments invoice: {str(e)}")
            raise
    
    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """
        IPN 웹훅 서명 검증
        
        Args:
            payload: 요청 본문
            signature: 헤더의 서명
            
        Returns:
            서명이 유효한지 여부
        """
        if not self.ipn_secret:
            logger.error("NOWPAYMENTS_IPN_SECRET not configured")
            return False
            
        # HMAC-SHA512으로 서명 생성
        computed_signature = hmac.new(
            self.ipn_secret.encode('utf-8'),
            payload,
            hashlib.sha512
        ).hexdigest()
        
        # 비교
        return hmac.compare_digest(computed_signature, signature)
    
    async def process_webhook(self, webhook_data: Dict[str, Any], db: AsyncSession):
        """
        웹훅 데이터 처리
        
        Args:
            webhook_data: 웹훅에서 받은 데이터
            db: 데이터베이스 세션
        """
        payment_id = webhook_data.get('payment_id')
        status = webhook_data.get('payment_status')
        paid_amount = float(webhook_data.get('paid_amount', 0))
        pay_currency = webhook_data.get('pay_currency', '').lower()
        order_id = webhook_data.get('order_id')
        
        logger.info(f"Processing NOWPayments webhook for payment_id: {payment_id}, status: {status}")
        
        # 주문 ID에서 테넌트 ID와 플랜 ID 추출
        # 예: tenant_abc123_plan_pro_1234567890
        parts = order_id.split('_')
        if len(parts) < 4:
            logger.error(f"Invalid order_id format: {order_id}")
            return
        
        tenant_id = parts[1]
        plan_id = parts[3] if len(parts) > 3 else parts[2]  # 호환성을 위해 두 가지 형식 지원
        
        # 기존 거래 조회
        existing_transaction = await db.execute(
            select(NowPaymentsTransaction).where(NowPaymentsTransaction.payment_id == payment_id)
        )
        transaction = existing_transaction.scalar_one_or_none()
        
        if not transaction:
            logger.error(f"No existing transaction found for payment_id: {payment_id}")
            return
        
        # 중복 처리 방지 - 이미 완료된 상태면 종료
        if transaction.payment_status in ['finished', 'confirmed']:
            logger.info(f"Payment {payment_id} already processed with status {transaction.payment_status}")
            return
        
        # 거래 상태 업데이트
        transaction.payment_status = status
        transaction.paid_amount = paid_amount
        transaction.pay_currency = pay_currency
        
        # 결제 완료 상태인지 확인
        # (중복 방지는 위 transaction.payment_status 체크로 이미 처리됨 —
        # 같은 payment_id가 finished/confirmed로 다시 들어오면 위에서 return됨)
        if status in ['finished', 'confirmed']:
            # 금액 검증
            plan = get_plan(plan_id)
            if not plan:
                logger.error(f"Invalid plan_id: {plan_id}")
                return
                
            expected_amount = plan.price_usd
            if abs(paid_amount - expected_amount) > 0.01:  # 소수점 오차 허용
                logger.error(f"Amount mismatch for payment {payment_id}. Expected: {expected_amount}, Paid: {paid_amount}")
                # 결제는 완료되었지만 금액이 일치하지 않음 - 수동 검토 필요
                transaction.note = f"Amount mismatch. Expected: {expected_amount}, Paid: {paid_amount}"
                await db.commit()
                return
            
            # 테넌트 조회
            tenant_result = await db.execute(
                select(Tenant).where(Tenant.id == tenant_id)
            )
            tenant = tenant_result.scalar_one_or_none()
            
            if not tenant:
                logger.error(f"Tenant not found: {tenant_id}")
                return
            
            # 플랜 적용
            await activate_tenant_plan(db, tenant_id, plan_id)
            
            logger.info(f"Successfully processed payment {payment_id} for tenant {tenant_id}, plan {plan_id}")
        
        await db.commit()


# 전역 인스턴스
_nowpayments_service = None


def get_nowpayments_service() -> NOWPaymentsService:
    global _nowpayments_service
    if _nowpayments_service is None:
        _nowpayments_service = NOWPaymentsService()
    return _nowpayments_service