"""Team /         , ,  ."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TeamMember(Base):
    """   Owner/Admin/Member  ,   .

     Tenant()     ,   role  
       .
    - owner:  ,  / 
    - admin:  /,    
    - member: /     (  )
    """

    __tablename__ = "team_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)

    #   
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    #  (Owner/Admin/Member)
    # - owner:  ,  
    # - admin:    ( /,  )
    # - member:   (//  )
    role: Mapped[str] = mapped_column(String(20), default="member")  # owner, admin, member

    # 
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    #  
    invited_by: Mapped[str | None] = mapped_column(String(36), nullable=True)  #   ID
    invite_token: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    invite_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # / 
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())