import logging

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import Subscription, User
from app.schemas.subscriptions import (
    EntitlementsResponse,
    MySubscriptionResponse,
    PlanResponse,
    PlansResponse,
    PlayVerifyRequest,
    PlayVerifyResponse,
    SubscriptionResponse,
)
from app.services import entitlements, play_sync
from app.services.play_billing import (
    PlayClient,
    PlayError,
    PlayPurchaseInvalid,
    decode_rtdn,
)

logger = logging.getLogger("app.play")

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


def plan_to_response(plan: object) -> PlanResponse:
    return PlanResponse.model_validate(plan, from_attributes=True)


def subscription_to_response(subscription: Subscription) -> SubscriptionResponse:
    return SubscriptionResponse.model_validate(subscription, from_attributes=True)


@router.get("/plans", response_model=PlansResponse)
async def list_plans(db: AsyncSession = Depends(get_db)) -> PlansResponse:
    plans = await repo.list_active_plans(db)
    return PlansResponse(plans=[plan_to_response(plan) for plan in plans])


@router.get("/me", response_model=MySubscriptionResponse)
async def my_subscription(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MySubscriptionResponse:
    effective = await entitlements.sync_for_user(db, user.user_id, request.app.state.settings)
    subscription = await repo.get_active_subscription(db, user.user_id)
    return MySubscriptionResponse(
        subscription=subscription_to_response(subscription) if subscription else None,
        entitlements=EntitlementsResponse(
            agent_access=effective.agent_access,
            cloud_agent_allowed=effective.cloud_agent_allowed,
            max_agent_messages_per_day=effective.max_agent_messages_per_day,
            plan_code=effective.plan_code,
            subscription_status=effective.subscription_status,
            current_period_end=effective.current_period_end,
            subscription_required=effective.subscription_required,
        ),
    )


def _entitlements_response(effective: entitlements.EffectiveEntitlements) -> EntitlementsResponse:
    return EntitlementsResponse(
        agent_access=effective.agent_access,
        cloud_agent_allowed=effective.cloud_agent_allowed,
        max_agent_messages_per_day=effective.max_agent_messages_per_day,
        plan_code=effective.plan_code,
        subscription_status=effective.subscription_status,
        current_period_end=effective.current_period_end,
        subscription_required=effective.subscription_required,
    )


@router.post("/play/verify", response_model=PlayVerifyResponse)
async def verify_play_purchase(
    request: Request,
    body: PlayVerifyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PlayVerifyResponse:
    """Turn a Play purchase token into an entitlement.

    Called by the app straight after Play reports a successful purchase. The
    token is resolved against Google before anything is written — the client's
    word that it paid is not evidence, and this endpoint is reachable by anyone
    with an access token.
    """
    play: PlayClient = request.app.state.play_client
    settings = request.app.state.settings

    if not play.configured:
        raise ApiError(
            503,
            "PLAY_NOT_CONFIGURED",
            "Purchase verification is unavailable. Nothing was charged twice; "
            "your purchase is safe and will be applied once this is restored.",
            retryable=True,
        )

    try:
        purchase = await play.get_subscription(body.purchase_token)
    except PlayPurchaseInvalid as exc:
        raise ApiError(400, "PLAY_PURCHASE_INVALID", str(exc)) from exc
    except PlayError as exc:
        logger.error("play verification failed: %s", exc)
        raise ApiError(
            503,
            "PLAY_UNAVAILABLE",
            "Could not reach Google Play to confirm the purchase. Try again shortly.",
            retryable=True,
        ) from exc

    if body.base_plan_id and purchase.base_plan_id and body.base_plan_id != purchase.base_plan_id:
        # Not fatal — Google's answer is what we act on — but a mismatch means
        # the client and Play disagree about what was bought, which is worth
        # seeing in the logs before it becomes a support ticket.
        logger.warning(
            "client reported base plan %s, Play reported %s",
            body.base_plan_id,
            purchase.base_plan_id,
        )

    try:
        subscription = await play_sync.apply_purchase(
            db, user_id=user.user_id, purchase=purchase
        )
    except play_sync.PlayOwnershipConflict as exc:
        raise ApiError(409, "PLAY_PURCHASE_ALREADY_LINKED", str(exc)) from exc
    except PlayPurchaseInvalid as exc:
        raise ApiError(400, "PLAY_PURCHASE_INVALID", str(exc)) from exc

    # Acknowledge only after the entitlement is durably ours to give. Play
    # auto-refunds anything unacknowledged for three days, which is the correct
    # outcome if we could not record the purchase.
    if not purchase.acknowledged:
        try:
            await play.acknowledge(purchase.product_id, purchase.purchase_token)
        except PlayError as exc:
            # The user has their subscription either way; a missed acknowledge
            # is recoverable on the next verify or RTDN, so it must not fail
            # the request they are waiting on.
            logger.error("failed to acknowledge %s: %s", purchase.product_id, exc)

    effective = await entitlements.sync_for_user(db, user.user_id, settings)
    return PlayVerifyResponse(
        subscription=subscription_to_response(subscription),
        entitlements=_entitlements_response(effective),
    )


@router.post("/play/rtdn", include_in_schema=False)
async def play_rtdn(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    """Real-time developer notifications from Google Play.

    Renewals, cancellations, refunds, revocations, grace periods and recoveries
    all arrive here. Without it a subscription would look valid until the app
    happened to re-verify, so a refunded user keeps access indefinitely.

    Pub/Sub cannot be authenticated by source address, so the push subscription
    must carry the configured shared secret. An unauthenticated webhook here
    would let anyone forge a renewal for any token they could guess.
    """
    settings = request.app.state.settings
    if not settings.play_rtdn_secret:
        raise ApiError(
            503, "PLAY_RTDN_NOT_CONFIGURED", "RTDN endpoint is not configured."
        )
    expected = f"Bearer {settings.play_rtdn_secret}"
    if authorization != expected:
        raise ApiError(401, "UNAUTHORIZED", "Invalid RTDN credentials.")

    body = await request.json()
    notification = decode_rtdn(body)
    if notification is None:
        # Test pings and notification kinds we do not act on. Answering 200
        # stops Pub/Sub redelivering something that will never be accepted.
        return {"status": "ignored"}

    if notification.package_name and notification.package_name != settings.play_package_name:
        logger.warning("RTDN for foreign package %s", notification.package_name)
        return {"status": "ignored"}

    play: PlayClient = request.app.state.play_client
    try:
        purchase = await play.get_subscription(notification.purchase_token)
    except PlayPurchaseInvalid as exc:
        logger.info("RTDN for unusable token: %s", exc)
        return {"status": "ignored"}
    except PlayError as exc:
        # Do NOT swallow this. A 5xx makes Pub/Sub redeliver, which is exactly
        # what should happen when Google was briefly unreachable — returning
        # 200 here would drop the event permanently.
        logger.error("RTDN resolution failed: %s", exc)
        raise ApiError(
            503, "PLAY_UNAVAILABLE", "Could not resolve the purchase.", retryable=True
        ) from exc

    try:
        await play_sync.apply_purchase(db, user_id=None, purchase=purchase)
    except PlayPurchaseInvalid as exc:
        logger.info("RTDN not applied: %s", exc)
        return {"status": "ignored"}

    logger.info(
        "RTDN %s applied for token ending %s",
        notification.notification_type,
        notification.purchase_token[-8:],
    )
    return {"status": "applied"}
