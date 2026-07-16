from pydantic import BaseModel, Field


class ChannelHubButton(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1)


class ChannelHubPublishRequest(BaseModel):
    account_id: str
    channel_id: str = Field(min_length=1, description="Chat id (e.g. -1001234567890) or @username")
    title: str = Field(min_length=1, max_length=4096)
    body: str = ""
    buttons: list[ChannelHubButton] = []
    pin_message: bool = False


class ChannelHubPublishResponse(BaseModel):
    id: str
    message_id: int
    published_at: str
    pinned: bool
