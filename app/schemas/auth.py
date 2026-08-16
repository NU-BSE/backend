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

    # Set only for accounts that sign in without a code — the credentials given
    # to store reviewers, who cannot read the mailbox the code would go to.
    # When true, the session below is already valid and there is no challenge
    # to answer; `challenge_id` is empty because none was created.
    #
    # Defaults to false, so an ordinary account, an older server, or any
    # response missing the field all mean "a code was sent, go ask for it".
    auto_verified: bool = False
    access_token: str | None = None
    refresh_token: str | None = None
    onboarding_completed: bool | None = None


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
