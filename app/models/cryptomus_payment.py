import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CryptomusPayment(Base):
    """Cryptomus crypto payment record.

    Isolated from the existing USDT/TronGrid payment path.
    """

    __tablename__ = "cryptomus_payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    invoice_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    order_id: Mapped[str] = mapped_column(String(100), index=True)

    plan: Mapped[str] = mapped_column(String(20))
    network: Mapped[str] = mapped_column(String(20))
    amount_usd: Mapped[float] = mapped_column(String(20))
    currency: Mapped[str] = mapped_column(String(10), default="USDT")

    status: Mapped[str] = mapped_column(String(20), default="pending")
    payment_address: Mapped[str | None] = mapped_column(String(200), nullable=True)
    qr_code_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[str | None] = mapped_column(String(50), nullable=True)

    paid_amount: Mapped[str | None] = mapped_column(String(50), nullable=True)
    paid_currency: Mapped[str | None] = mapped_column(String(20), nullable=True)
    issued_api_key: Mapped[str | None] = mapped_column(String(100), nullable=True)

    webhook_received_at: Mapped[str | None] = mapped_column(String(50), nullable=True)
    processed_at: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
