from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class TelegramDialog(BaseModel):
    id: int
    title: str
    type: str  # "private", "group", "megagroup", "channel"
    unread_count: int
    last_message: str | None = None
    last_message_date: datetime | None = None
    pinned: bool = False
    photo: str | None = None
    participants_count: int = 0
    username: str | None = None

    model_config = {"from_attributes": True}


class TelegramMessage(BaseModel):
    id: int
    sender_id: int | None = None
    sender_name: str | None = None
    text: str
    date: datetime | None = None
    is_outgoing: bool = False
    reply_to_msg_id: int | None = None
    reply_to_text: str | None = None
    media_type: str | None = None  # "photo", "video", "document", etc.
    media_file_id: str | None = None
    is_forwarded: bool = False
    forward_from_name: str | None = None

    model_config = {"from_attributes": True}


class SendMessageRequest(BaseModel):
    text: str
    reply_to_msg_id: int | None = None
    media_path: str | None = None


class SendMessageResponse(BaseModel):
    message_id: int
    status: str


class ChatListResponse(BaseModel):
    accounts: list[dict]  # simplified account list
    dialogs: list[TelegramDialog]
