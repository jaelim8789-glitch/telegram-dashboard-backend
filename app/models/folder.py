import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Folder(Base):
    __tablename__ = "folders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    color: Mapped[str] = mapped_column(String(20), default="#6366f1")
    icon: Mapped[str] = mapped_column(String(50), default="folder")

    # JSON-encoded list[str] of Telegram chat ids — matches the Text+json.dumps
    # convention used for ReplyMacro.target_chats rather than a native JSON column.
    group_ids: Mapped[str] = mapped_column(Text, default="[]")

    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("folders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_collapsed: Mapped[bool] = mapped_column(Boolean, default=False)

    is_smart: Mapped[bool] = mapped_column(Boolean, default=False)
    smart_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    smart_params: Mapped[str] = mapped_column(Text, default="{}")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    @property
    def order(self) -> int:
        """Alias so FolderRead (from_attributes) can expose `order` — the field name
        the frontend contract uses — without a DB column named `order` (a SQL keyword)."""
        return self.sort_order
