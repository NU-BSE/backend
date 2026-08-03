import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_email_code_service, get_email_sender
from app.core.config import Settings
from app.core.errors import ApiError
from app.core.security import (
    TOKEN_TYPE_REFRESH,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.db import repositories as repo
from app.db.models import User
from app.db.repositories import lifetime_period_end
from app.db.schema import SEED_PLANS
from app.schemas.auth import (
    RefreshRequest,
    RefreshResponse,
    RequestCodeRequest,
    RequestCodeResponse,
    VerifyCodeRequest,
    VerifyCodeResponse,
)
from app.services import entitlements
from app.services.email_codes import EmailCodeService
from app.services.email_sender import EmailSender

logger = logging.getLogger("app.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.post("/email/request-code", response_model=RequestCodeResponse)
async def request_code(
    request: Request,
    body: RequestCodeRequest,
    codes: EmailCodeService = Depends(get_email_code_service),
    sender: EmailSender = Depends(get_email_sender),
) -> RequestCodeResponse:
    email = body.email.lower()
    result = await codes.request_code(
        email=email,
        name=body.name,
        purpose=body.purpose,
        client_ip=_client_ip(request),
    )
    try:
        await sender.send_verification_code(to=email, code=result.code, purpose=body.purpose)
    except Exception as exc:
        logger.error("failed to send verification code: %s", exc)
        raise ApiError(
            502,
            "EMAIL_SEND_FAILED",
            "Could not send the verification code. Please try again.",
            retryable=True,
        ) from exc

    return RequestCodeResponse(
        challenge_id=result.challenge_id,
        expires_in_seconds=result.expires_in_seconds,
        retry_after_seconds=result.retry_after_seconds,
    )


@router.post("/email/verify-code", response_model=VerifyCodeResponse)
async def verify_code(
    request: Request,
    body: VerifyCodeRequest,
    db: AsyncSession = Depends(get_db),
    codes: EmailCodeService = Depends(get_email_code_service),
) -> VerifyCodeResponse:
    settings = _settings(request)
    email = body.email.lower()
    challenge = await codes.verify_code(
        challenge_id=body.challenge_id, email=email, code=body.code
    )

    user = await repo.get_user_by_email(db, email)
    if user is None:
        user = await repo.create_user(db, email=email, name=challenge.name)
        await _grant_free_plan(db, user)
        logger.info("registered user %s", user.user_id)
    else:
        logger.info("logged in user %s", user.user_id)

    access_token = create_access_token(settings, user.user_id, email)
    refresh_token = create_refresh_token(settings, user.user_id, email)
    return VerifyCodeResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        onboarding_completed=user.name is not None,
        email=email,
    )


async def _grant_free_plan(db: AsyncSession, user: User) -> None:
    free = next(plan for plan in SEED_PLANS if plan["code"] == "free")
    plan = await repo.get_plan_by_code(db, str(free["code"]))
    if plan is None:
        return

    now = datetime.now(timezone.utc)
    subscription = await repo.create_subscription(
        db,
        user_id=user.user_id,
        plan_id=plan.plan_id,
        status="active",
        provider="manual",
        provider_subscription_id=None,
        period_start=now,
        period_end=lifetime_period_end(),
    )
    await entitlements.materialize(
        db,
        user_id=user.user_id,
        plan=plan,
        expires_at=subscription.current_period_end,
        source="grant",
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(
    request: Request,
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> RefreshResponse:
    settings = _settings(request)
    subject = decode_token(settings, body.refresh_token, TOKEN_TYPE_REFRESH)
    user = await repo.get_user_by_id(db, subject.user_id)
    if user is None:
        raise ApiError(401, "UNAUTHORIZED", "User no longer exists")
    email = user.email or subject.email
    return RefreshResponse(access_token=create_access_token(settings, user.user_id, email))


@router.post("/logout")
async def logout() -> dict[str, bool]:
    # Stateless JWTs: the client discards its tokens. Endpoint exists so the
    # client has a stable contract (and so we can add revocation later).
    return {"ok": True}
