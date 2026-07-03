from typing import Literal

from pydantic import BaseModel

GroupType = Literal["group", "megagroup", "channel"]


class GroupRead(BaseModel):
    id: str
    title: str
    type: GroupType
    participants_count: int | None = None
