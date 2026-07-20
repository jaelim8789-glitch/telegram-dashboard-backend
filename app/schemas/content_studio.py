"""AI Content Studio Pydantic schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ContentType = Literal["promotional", "announcement", "engagement", "informational", "testimonial", "event"]
ContentTone = Literal["short", "emotional", "intense"]


class ContentGenerateRequest(BaseModel):
    content_type: str = Field(
        ...,
        pattern=r"^(promotional|announcement|engagement|informational|testimonial|event)$",
        description="콘텐츠 타입",
    )
    tone: str = Field(
        ...,
        pattern=r"^(short|emotional|intense)$",
        description="메시지 톤",
    )
    topic: str | None = Field(default=None, max_length=500, description="선택적 주제/키워드")
    context: str | None = Field(default=None, max_length=1000, description="추가 컨텍스트 (상품/서비스 정보 등)")
    style_profile_id: str | None = Field(default=None, description="스타일 프로필 ID (선택적)")


class ContentGenerateResponse(BaseModel):
    content_type: str
    tone: str
    generated_content: str
    tokens_used: int
    style_profile_id: str | None = None
    content_studio_content_id: str | None = None


class ContentCalendarSettingCreate(BaseModel):
    account_id: str
    enabled: bool = False
    daily_count: int = Field(ge=1, le=10, description="하루 자동 생성 개수")
    content_types: list[str] = Field(
        min_length=1,
        max_length=6,
        description="자동 생성할 콘텐츠 타입 목록",
    )
    tone: str = Field(default="short", pattern=r"^(short|emotional|intense)$")
    group_ids: list[str] = Field(min_length=1, description="발송 대상 그룹 ID 목록")
    timezone: str = Field(default="Asia/Seoul", max_length=50)
    send_hour: int = Field(ge=0, le=23, description="하루 중 발송 시각 (0-23)")


class ContentCalendarSettingUpdate(BaseModel):
    enabled: bool | None = None
    daily_count: int | None = Field(default=None, ge=1, le=10)
    content_types: list[str] | None = Field(default=None, min_length=1, max_length=6)
    tone: str | None = Field(default=None, pattern=r"^(short|emotional|intense)$")
    group_ids: list[str] | None = Field(default=None, min_length=1)
    timezone: str | None = Field(default=None, max_length=50)
    send_hour: int | None = Field(default=None, ge=0, le=23)


class ContentCalendarSettingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    account_id: str
    tenant_id: str
    enabled: bool
    daily_count: int
    content_types: list[str]
    tone: str
    group_ids: list[str]
    timezone: str
    send_hour: int
    last_generated_at: datetime | None
    next_generate_at: datetime | None
    created_at: datetime
    updated_at: datetime
