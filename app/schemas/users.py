from datetime import datetime

from pydantic import Field

from app.schemas.common import CamelModel


class UserResponse(CamelModel):
    user_id: str
    email: str | None
    name: str | None
    created_at: datetime


class UpdateUserRequest(CamelModel):
    name: str = Field(min_length=1, max_length=200)
