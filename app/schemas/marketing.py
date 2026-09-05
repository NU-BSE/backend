from typing import Literal

from pydantic import EmailStr, Field

from app.schemas.common import CamelModel


class DownloadInviteRequest(CamelModel):
    email: EmailStr
    billing: Literal["monthly", "annual"]
    source_path: str = Field(default="/", min_length=1, max_length=512)


class DownloadInviteResponse(CamelModel):
    ok: bool = True
    already_requested: bool = False
