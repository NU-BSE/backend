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
from app.services import entitlements, onboarding
from app.services.email_codes import EmailCodeService
from app.services.email_sender import EmailSender

logger = logging.getLogger("app.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# Distinguishes "not configured as a demo account" from "configured with no
# required name", which are different answers that a plain .get() would blur.
_NOT_A_DEMO_ACCOUNT = object()


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.post("/email/request-code", response_model=RequestCodeResponse)
async def request_code(
    request: Request,
    body: RequestCodeRequest,
    db: AsyncSession = Depends(get_db),
    codes: EmailCodeService = Depends(get_email_code_service),
    sender: EmailSender = Depends(get_email_sender),
) -> RequestCodeResponse:
    settings = _settings(request)
    email = body.email.lower()

    # The demo sign-in needs the exact name as well as the address. A mismatch
    # falls through to the ordinary code flow rather than erroring: a distinct
    # response would confirm to whoever is probing that the address is real.
    demo_name = settings.demo_account_map.get(email, _NOT_A_DEMO_ACCOUNT)
    if demo_name is not _NOT_A_DEMO_ACCOUNT and (
        demo_name is None or (body.name or "").strip() == demo_name
    ):
        return await _demo_sign_in(
            request, db, codes, email=email, name=body.name
        )

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
    was_new = user is None
    if user is None:
        user = await repo.create_user(db, email=email, name=challenge.name)
        await _grant_free_plan(db, user)
        logger.info("registered user %s", user.user_id)
    else:
        logger.info("logged in user %s", user.user_id)

    onboarding_state = await onboarding.complete_after_auth(
        db, user, was_new=was_new
    )

    access_token = create_access_token(settings, user.user_id, email)
    refresh_token = create_refresh_token(settings, user.user_id, email)
    return VerifyCodeResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        onboarding_completed=onboarding.is_completed(onboarding_state),
        email=email,
    )


async def _demo_sign_in(
    request: Request,
    db: AsyncSession,
    codes: EmailCodeService,
    *,
    email: str,
    name: str | None,
) -> RequestCodeResponse:
    """Sign a demo account in without a code.

    Store review needs working credentials, and a reviewer cannot read the
    mailbox a verification code would be sent to. For addresses in
    DEMO_ACCOUNTS the code step is therefore skipped entirely: no code is
    generated, none is emailed, and the session is issued here.

    This is a real bypass and worth being clear about — the address alone is
    the credential, with no second factor. It is off unless DEMO_ACCOUNTS is
    set, it should be set only on the deployment reviewers use, and the address
    should be treated as a published secret.

    Rate limiting still applies, minus two limits that would backfire:

    - the per-email cap is skipped, because a single shared address would
      otherwise let anyone lock the reviewer out with five requests;
    - the resend cooldown is skipped, because there is no resend, and a
      reviewer relaunching the app should not meet a 60-second wall.

    The per-IP hourly cap stays, so this cannot be used to hammer the service.
    """
    settings = _settings(request)
    await codes.enforce_request_limits(
        email=email,
        client_ip=_client_ip(request),
        apply_cooldown=False,
        apply_email_cap=False,
    )

    user = await repo.get_user_by_email(db, email)
    was_new = user is None
    if user is None:
        user = await repo.create_user(db, email=email, name=name)
        await _grant_free_plan(db, user)
        logger.warning("demo sign-in: registered %s (%s)", email, user.user_id)
    else:
        logger.warning("demo sign-in: %s (%s)", email, user.user_id)

    onboarding_state = await onboarding.complete_after_auth(
        db, user, was_new=was_new
    )

    return RequestCodeResponse(
        # No challenge was created; there is nothing to answer.
        challenge_id="",
        expires_in_seconds=0,
        retry_after_seconds=0,
        auto_verified=True,
        access_token=create_access_token(settings, user.user_id, email),
        refresh_token=create_refresh_token(settings, user.user_id, email),
        onboarding_completed=onboarding.is_completed(onboarding_state),
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
