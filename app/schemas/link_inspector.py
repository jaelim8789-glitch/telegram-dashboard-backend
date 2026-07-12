from pydantic import BaseModel, Field


class LinkInspectRequest(BaseModel):
    account_id: str
    links: list[str] = Field(min_length=1, max_length=100, description="Raw t.me links, invite links, or @usernames")


class LinkInspectionItem(BaseModel):
    raw_link: str
    status: str  # active | private | dead | flood_wait | error
    accessible: bool
    title: str | None = None
    chat_type: str | None = None  # group | megagroup | channel
    username: str | None = None
    chat_id: str | None = None
    participants_count: int | None = None
    reason: str | None = None


class LinkInspectResponse(BaseModel):
    items: list[LinkInspectionItem]
    total_submitted: int
    duplicates_removed: int
    total_inspected: int


class LinkJoinTarget(BaseModel):
    """Identifies one inspected link to join — echoed back by the frontend from a
    prior /inspect response so we don't need to persist inspection results.

    Carries raw_link (re-parsed at join time) rather than username/chat_id: an
    invite link not yet joined has neither (CheckChatInviteRequest doesn't
    resolve a chat_id until the account actually joins), so the raw link is
    the only identifier guaranteed to be present for every active result.
    """
    raw_link: str
    title: str = ""


class JoinLinksRequest(BaseModel):
    account_id: str
    targets: list[LinkJoinTarget] = Field(min_length=1, max_length=50)


class LinkJoinResultItem(BaseModel):
    chat_id: str | None
    title: str
    success: bool
    error: str | None = None


class LinkJoinResponse(BaseModel):
    items: list[LinkJoinResultItem]
