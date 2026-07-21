from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ConversationRead(BaseModel):
    id: str
    title: str
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class MessageRead(BaseModel):
    id: str
    role: str
    content: str
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class ConversationCreateResponse(BaseModel):
    id: str


class MessageAddResponse(BaseModel):
    id: str


class StatusResponse(BaseModel):
    status: str
