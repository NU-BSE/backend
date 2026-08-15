"""Reconcile a Play purchase token with our subscription rows.

One function does the writing for both entry points — the client calling
/subscriptions/play/verify after a purchase, and Google calling the RTDN
webhook for every later event. Both resolve the token against Play first and
then apply the same rules, so a renewal that arrives by webhook and one
discovered by the app produce identical state. Two code paths writing
subscriptions from the same facts is how the two drift.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repositories as repo
from app.db.models import Subscription, SubscriptionPlan
from app.db.schema import PLAY_BASE_PLAN_TO_CODE
from app.services import entitlements
from app.services.play_billing import (
    ENTITLING_STATUSES,
    PlayPurchaseInvalid,
    PlaySubscription,
)

logger = logging.getLogger("app.play")

PROVIDER = "google_play"


class PlayOwnershipConflict(Exception):
    """The token is already held by a different account."""


async def _subscription_for_token(
    session: AsyncSession, purchase_token: str
) -> Subscription | None:
    result = await session.execute(
        select(Subscription).where(
            Subscription.provider == PROVIDER,
            Subscription.provider_subscription_id == purchase_token,
        )
    )
    return result.scalar_one_or_none()


async def _plan_for(session: AsyncSession, purchase: PlaySubscription) -> SubscriptionPlan:
    code = PLAY_BASE_PLAN_TO_CODE.get(purchase.base_plan_id or "")
    if code is None:
        raise PlayPurchaseInvalid(
            f"Play base plan {purchase.base_plan_id!r} is not in the catalogue."
        )
    plan = await repo.get_plan_by_code(session, code)
    if plan is None:
        raise PlayPurchaseInvalid(f"No plan seeded for code {code!r}.")
    return plan


async def apply_purchase(
    session: AsyncSession,
    *,
    user_id: str | None,
    purchase: PlaySubscription,
) -> Subscription:
    """Write Play's view of a purchase into our subscription rows.

    `user_id` is the caller's identity when the app is verifying, and None on
    the RTDN path, where Google tells us the token but not who owns it — the
    owner is then whoever already holds the row.
    """
    existing = await _subscription_for_token(session, purchase.purchase_token)

    if user_id is None:
        if existing is None:
            # A notification for a token we have never seen. This is normal
            # exactly once: the RTDN for a brand-new purchase can beat the
            # app's own verify call. The app will present the token shortly.
            raise PlayPurchaseInvalid(
                "Notification for an unknown purchase token; awaiting client verification."
            )
        owner_id = existing.user_id
    else:
        if existing is not None and existing.user_id != user_id:
            # A purchase token is bearer proof of payment. Letting a second
            # account present it would move one paid subscription onto many.
            raise PlayOwnershipConflict(
                "This purchase is already linked to a different account."
            )
        owner_id = user_id

    plan = await _plan_for(session, purchase)

    # Play reissues the token on an upgrade or downgrade and points the new one
    # at the old via linkedPurchaseToken. The superseded row must stop
    # entitling, or the user holds two live subscriptions after switching plan.
    if purchase.linked_purchase_token:
        superseded = await _subscription_for_token(session, purchase.linked_purchase_token)
        if superseded is not None and superseded.subscription_id != (
            existing.subscription_id if existing else None
        ):
            await repo.cancel_subscription(session, superseded)
            superseded.status = "expired"
            superseded.ended_at = datetime.now(timezone.utc)
            logger.info(
                "superseded play subscription %s for user %s",
                superseded.subscription_id,
                owner_id,
            )

    if existing is not None:
        existing.plan_id = plan.plan_id
        existing.status = purchase.status
        existing.current_period_start = purchase.start
        existing.current_period_end = purchase.expiry
        existing.cancel_at_period_end = not purchase.auto_renewing
        if purchase.status in {"canceled", "expired"} and existing.canceled_at is None:
            existing.canceled_at = datetime.now(timezone.utc)
        if purchase.status == "expired":
            existing.ended_at = purchase.expiry
        existing.updated_at = datetime.now(timezone.utc)
        subscription = existing
    else:
        # Any other subscription this user holds (a manual grant, an older
        # purchase) is closed first, so "active subscription" stays singular.
        previous = await repo.get_active_subscription(session, owner_id)
        if previous is not None:
            await repo.cancel_subscription(session, previous)

        subscription = await repo.create_subscription(
            session,
            user_id=owner_id,
            plan_id=plan.plan_id,
            status=purchase.status,
            provider=PROVIDER,
            provider_subscription_id=purchase.purchase_token,
            period_start=purchase.start,
            period_end=purchase.expiry,
        )
        subscription.cancel_at_period_end = not purchase.auto_renewing

    # Entitlements expire with the paid period. A lapsed subscription therefore
    # stops conferring anything without needing a sweep job: the row's own
    # expiry is the gate.
    if purchase.status in ENTITLING_STATUSES and purchase.expiry > datetime.now(timezone.utc):
        await entitlements.materialize(
            session,
            user_id=owner_id,
            plan=plan,
            expires_at=purchase.expiry,
            source="subscription",
        )
    else:
        await repo.clear_entitlements(session, owner_id)

    await session.flush()
    return subscription
