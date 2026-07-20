from datetime import datetime

from pydantic import BaseModel, Field


class StyleProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_type: str = Field(default="text", pattern="^(url|text)$")
    source_text: str = Field(min_length=10, max_length=50000)


class StyleProfileAnalyzeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_type: str = Field(default="text", pattern="^(url|text)$")
    source_text: str = Field(min_length=10, max_length=50000)


class StyleProfileUpdate(BaseModel):
    name: str | None = Field(None, max_length=200)


class StyleProfileRead(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str
    source_type: str
    source_text: str
    tone_analysis: dict
    style_prompt: str
    created_at: datetime
    updated_at: datetime
