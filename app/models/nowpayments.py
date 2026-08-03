"""
NOWPayments   - NOWPayments   
"""

from datetime import datetime
from sqlalchemy import Column, String, Float, DateTime, Text, Boolean
from sqlalchemy.sql import func

from app.database import Base


class NowPaymentsTransaction(Base):
    __tablename__ = "nowpayments_transactions"

    #  ID
    id = Column(String, primary_key=True, index=True)
    
    # NOWPayments    ID
    payment_id = Column(String, unique=True, index=True, nullable=False)
    
    #   ID
    tenant_id = Column(String, index=True, nullable=False)
    
    #   ID
    plan_id = Column(String, nullable=False)
    
    #  
    amount = Column(Float, nullable=False)
    
    #  
    pay_currency = Column(String, nullable=False)
    
    #  
    paid_amount = Column(Float, nullable=True)
    
    #  ID (    )
    order_id = Column(String, index=True, nullable=False)
    
    #  
    payment_status = Column(String, index=True, nullable=False)
    
    #  :  (  false->   true  )
    fulfilled = Column(Boolean, default=False, nullable=False)
    
    #  (  )
    fulfilled_at = Column(DateTime, nullable=True)
    
    # / (:    )
    note = Column(Text, nullable=True)
    
    #  
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    
    #  
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())