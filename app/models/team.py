"""Team / 멤버 관리 모델 — 팀 단위 운영을 위한 초대, 역할, 권한 관리."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TeamMember(Base):
    """팀 멤버 — Owner/Admin/Member 역할 구분, 초대 기반 가입.

    하나의 Tenant(팀)에 여러 멤버가 속할 수 있으며, 각 멤버는 role 에 따라
    기능 접근 권한이 달라집니다.
    - owner: 모든 권한, 팀 삭제/양도 가능
    - admin: 멤버 초대/관리, 대부분의 설정 변경 가능
    - member: 계정/발송 등 운영 기능 사용 (설정 변경 불가)
    """

    __tablename__ = "team_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)

    # 멤버 식별 정보
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # 역할 (Owner/Admin/Member)
    # - owner: 테넌트 생성자, 최고 관리자
    # - admin: 팀 관리 권한 (멤버 초대/추방, 설정 변경)
    # - member: 일반 운영자 (계정/발송/캠페인 등 사용)
    role: Mapped[str] = mapped_column(String(20), default="member")  # owner, admin, member

    # 상태
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # 초대 관련
    invited_by: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 초대한 멤버 ID
    invite_token: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    invite_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 활동
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 생성/수정 시간
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())