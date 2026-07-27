"""
NOWPayments Service  Crypto payment processing service.
Webhook data handling and payment processing.
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
from app.core.time import utcnow_naive
from app.models.nowpayments import NowPaymentsTransaction
from app.models.tenant import Tenant
from app.services.cryptomus import activate_tenant_plan
from app.services.referral import create_commission

logger = get_logger(__name__)


class NOWPaymentsService:
    def __init__(self):
        self.api_key = settings.NOWPAYMENTS_API_KEY
        self.public_key = settings.NOWPAYMENTS_PUBLIC_KEY
        self.ipn_secret = settings.NOWPAYMENTS_IPN_SECRET
        self.base_url = "https://api.nowpayments.io/v1"
        
    async def create_payment(self, amount: float, currency: str, plan_id: str, tenant_id: str, order_description: str = "TeleMon Subscription"):
        """
        NOWPayments    
        
        Args:
            amount:  
            currency:  (: usdt, btc, eth )
            plan_id:  ID
            tenant_id:  ID
            order_description:  
            
        Returns:
              
        """
        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json"
        }
        
        payload = {
            "price_amount": amount,
            "price_currency": "usd",  #  USD 
            "pay_currency": currency.lower(),
            "order_id": f"tenant_{tenant_id}_plan_{plan_id}_{int(datetime.now().timestamp())}",
            "order_description": order_description,
            "ipn_callback_url": f"{settings.api_base_url}/api/payments/nowpayments/webhook",
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
                
                transaction = NowPaymentsTransaction(
                    id=result['payment_id'],
                    payment_id=result['payment_id'],
                    tenant_id=tenant_id,
                    plan_id=plan_id,
                    amount=result['price_amount'],
                    pay_currency=result['pay_currency'],
                    order_id=result['order_id'],
                    payment_status='created',
                    created_at=utcnow_naive()
                )
                from app.database import async_session_maker
                async with async_session_maker() as db:
                    db.add(transaction)
                    await db.commit()
                
                return result
        except Exception as e:
            logger.error(f"Error creating NOWPayments invoice: {str(e)}")
            raise
    
    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """
        IPN   
        
        Args:
            payload:  
            signature:  
            
        Returns:
              
        """
        if not self.ipn_secret:
            logger.error("NOWPAYMENTS_IPN_SECRET not configured")
            return False
            
        # HMAC-SHA512  
        computed_signature = hmac.new(
            self.ipn_secret.encode('utf-8'),
            payload,
            hashlib.sha512
        ).hexdigest()
        
        # 
        return hmac.compare_digest(computed_signature, signature)
    
    async def process_webhook(self, webhook_data: Dict[str, Any], db: AsyncSession):
        """
          
        
        Args:
            webhook_data:   
            db:  
        """
        payment_id = webhook_data.get('payment_id')
        status = webhook_data.get('payment_status')
        paid_amount = float(webhook_data.get('paid_amount', 0))
        pay_currency = webhook_data.get('pay_currency', '').lower()
        order_id = webhook_data.get('order_id')
        
        logger.info(f"Processing NOWPayments webhook for payment_id: {payment_id}, status: {status}")
        
        #  ID  ID  ID 
        # : tenant_abc123_plan_pro_1234567890
        parts = order_id.split('_')
        if len(parts) < 4:
            logger.error(f"Invalid order_id format: {order_id}")
            return
        
        tenant_id = parts[1]
        plan_id = parts[3] if len(parts) > 3 else parts[2]  #      
        
        #   
        existing_transaction = await db.execute(
            select(NowPaymentsTransaction).where(NowPaymentsTransaction.payment_id == payment_id)
        )
        transaction = existing_transaction.scalar_one_or_none()
        
        if not transaction:
            logger.error(f"No existing transaction found for payment_id: {payment_id}")
            return
        
        #    -    
        if transaction.payment_status in ['finished', 'confirmed']:
            logger.info(f"Payment {payment_id} already processed with status {transaction.payment_status}")
            return
        
        #   
        transaction.payment_status = status
        transaction.paid_amount = paid_amount
        transaction.pay_currency = pay_currency
        
        #    
        # (   transaction.payment_status    
        #  payment_id finished/confirmed    return)
        if status in ['finished', 'confirmed']:
            #  
            plan = get_plan(plan_id)
            if not plan:
                logger.error(f"Invalid plan_id: {plan_id}")
                return
                
            billing = "quarterly" if "quarterly" in plan["prices_usdt"] else "monthly"
            expected_amount = plan["prices_usdt"].get(billing, 0)
            if abs(paid_amount - expected_amount) > 0.01:  #   
                logger.error(f"Amount mismatch for payment {payment_id}. Expected: {expected_amount}, Paid: {paid_amount}")
                #      -   
                transaction.note = f"Amount mismatch. Expected: {expected_amount}, Paid: {paid_amount}"
                await db.commit()
                return
            
            #  
            tenant_result = await db.execute(
                select(Tenant).where(Tenant.id == tenant_id)
            )
            tenant = tenant_result.scalar_one_or_none()
            
            if not tenant:
                logger.error(f"Tenant not found: {tenant_id}")
                return
            
            #  
            await activate_tenant_plan(db, tenant_id, plan_id)

            #   
            amount_cents = int(paid_amount * 100)
            await create_commission(db, tenant_id, payment_id, "nowpayments", amount_cents)

            logger.info(f"Successfully processed payment {payment_id} for tenant {tenant_id}, plan {plan_id}")
        
        await db.commit()


#  
_nowpayments_service = None


def get_nowpayments_service() -> NOWPaymentsService:
    global _nowpayments_service
    if _nowpayments_service is None:
        _nowpayments_service = NOWPaymentsService()
    return _nowpayments_service