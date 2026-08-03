from typing import Literal

from pydantic import EmailStr, Field

from app.schemas.common import CamelModel


class RequestCodeRequest(CamelModel):
    email: EmailStr
    name: str | None = Field(default=None, max_length=200)
    purpose: Literal["registration", "login"]


class RequestCodeResponse(CamelModel):
    challenge_id: str
    expires_in_seconds: int
    retry_after_seconds: int


class VerifyCodeRequest(CamelModel):
    challenge_id: str
    code: str = Field(min_length=6, max_length=6, pattern="^[0-9]{6}$")
    email: EmailStr


class VerifyCodeResponse(CamelModel):
    access_token: str
    refresh_token: str
    onboarding_completed: bool
    email: str


class RefreshRequest(CamelModel):
    refresh_token: str


class RefreshResponse(CamelModel):
    access_token: str


class LogoutRequest(CamelModel):
    refresh_token: str | None = None
