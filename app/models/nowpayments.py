"""
NOWPayments 거래 모델 - NOWPayments 결제 트랜잭션 기록용
"""

from datetime import datetime
from sqlalchemy import Column, String, Float, DateTime, Text
from sqlalchemy.sql import func

from app.database import Base


class NowPaymentsTransaction(Base):
    __tablename__ = "nowpayments_transactions"

    # 고유 ID
    id: str = Column(String, primary_key=True, index=True)
    
    # NOWPayments에서 제공하는 고유 결제 ID
    payment_id: str = Column(String, unique=True, index=True, nullable=False)
    
    # 관련 테넌트 ID
    tenant_id: str = Column(String, index=True, nullable=False)
    
    # 관련 플랜 ID
    plan_id: str = Column(String, nullable=False)
    
    # 요청된 금액
    amount: float = Column(Float, nullable=False)
    
    # 결제 통화
    pay_currency: str = Column(String, nullable=False)
    
    # 결제된 금액
    paid_amount: float = Column(Float, nullable=True)
    
    # 주문 ID (고객 시스템 내 주문 식별자)
    order_id: str = Column(String, index=True, nullable=False)
    
    # 결제 상태
    payment_status: str = Column(String, index=True, nullable=False)
    
    # 메모/노트 (예: 금액 불일치 등의 정보)
    note: str = Column(Text, nullable=True)
    
    # 생성 시간
    created_at: datetime = Column(DateTime, server_default=func.now(), nullable=False)
    
    # 업데이트 시간
    updated_at: datetime = Column(DateTime, server_default=func.now(), onupdate=func.now())