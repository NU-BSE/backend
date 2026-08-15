"""Entitlement resolution.

The subscription rows are the source of truth; `subscription_entitlements`
is the materialized lookup the agent gate checks, so expiry and grants both
collapse into one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db import repositories as repo
from app.db.models import Subscription, SubscriptionPlan, User

logger = logging.getLogger("app.entitlements")

ENTITLEMENT_AGENT_ACCESS = "agent_access"
ENTITLEMENT_CLOUD_AGENT = "cloud_agent_allowed"


# What a demo account is given. Deliberately the Pro daily cap rather than an
# unlimited one: these credentials are published to reviewers and can leak, and
# a bounded allowance limits what a leak costs.
DEMO_PLAN_CODE = "demo"
DEMO_MAX_AGENT_MESSAGES_PER_DAY = 200


@dataclass(frozen=True)
class EffectiveEntitlements:
    agent_access: bool
    cloud_agent_allowed: bool
    max_agent_messages_per_day: int | None
    plan_code: str | None
    subscription_status: str | None
    current_period_end: datetime | None
    # False when this account never needs to buy anything — today, only a demo
    # account. The client reads this instead of recognising any particular
    # user, so the app has no idea which accounts are special and the answer
    # stays a server-side decision.
    subscription_required: bool = True


def is_demo_account(settings: Settings, user: User | None) -> bool:
    demo = settings.demo_account_set
    if not demo or user is None or not user.email:
        return False
    return user.email.strip().lower() in demo


def demo_entitlements() -> EffectiveEntitlements:
    """Full access with no subscription row and no expiry.

    Nothing is written to the subscription tables for these accounts. A demo
    grant is a property of the configuration, not of the billing record, so
    removing the address from DEMO_ACCOUNTS revokes it immediately and leaves
    no orphaned "active" subscription behind that nobody ever paid for.
    """
    return EffectiveEntitlements(
        agent_access=True,
        cloud_agent_allowed=True,
        max_agent_messages_per_day=DEMO_MAX_AGENT_MESSAGES_PER_DAY,
        plan_code=DEMO_PLAN_CODE,
        subscription_status="demo",
        current_period_end=None,
        subscription_required=False,
    )


async def materialize(
    session: AsyncSession,
    *,
    user_id: str,
    plan: SubscriptionPlan,
    expires_at: datetime | None,
    source: str,
) -> None:
    """Write the entitlement rows a plan confers."""
    grants: dict[str, datetime | None] = {}
    if plan.agent_access:
        grants[ENTITLEMENT_AGENT_ACCESS] = expires_at
    if plan.cloud_agent_allowed:
        grants[ENTITLEMENT_CLOUD_AGENT] = expires_at
    await repo.upsert_entitlements(session, user_id, grants, source)


async def sync_for_user(
    session: AsyncSession,
    user_id: str,
    settings: Settings,
) -> EffectiveEntitlements:
    """Resolve (and re-materialize) the effective rights for a user.

    Demo accounts short-circuit here, which is the whole reason every gate in
    the service resolves rights through this one function: the agent, the cloud
    agent and the model-weight download all inherit the exemption without any
    of them knowing that demo accounts exist.
    """
    user = await session.get(User, user_id)
    if is_demo_account(settings, user):
        logger.info("demo account %s: entitlements granted without billing", user_id)
        return demo_entitlements()

    subscription = await repo.get_active_subscription(session, user_id)
    if subscription is None:
        return EffectiveEntitlements(
            agent_access=False,
            cloud_agent_allowed=False,
            max_agent_messages_per_day=None,
            plan_code=None,
            subscription_status=None,
            current_period_end=None,
        )

    plan = await session.get(SubscriptionPlan, subscription.plan_id)
    if plan is None:
        return EffectiveEntitlements(
            agent_access=False,
            cloud_agent_allowed=False,
            max_agent_messages_per_day=None,
            plan_code=None,
            subscription_status=subscription.status,
            current_period_end=subscription.current_period_end,
        )

    await materialize(
        session,
        user_id=user_id,
        plan=plan,
        expires_at=subscription.current_period_end,
        source="grant" if subscription.provider == "manual" else "subscription",
    )

    active = await repo.get_active_entitlements(session, user_id)
    return EffectiveEntitlements(
        agent_access=ENTITLEMENT_AGENT_ACCESS in active,
        cloud_agent_allowed=ENTITLEMENT_CLOUD_AGENT in active,
        max_agent_messages_per_day=plan.max_agent_messages_per_day,
        plan_code=plan.code,
        subscription_status=subscription.status,
        current_period_end=subscription.current_period_end,
    )


async def active_subscription_with_plan(
    session: AsyncSession, user_id: str
) -> tuple[Subscription | None, SubscriptionPlan | None]:
    subscription = await repo.get_active_subscription(session, user_id)
    if subscription is None:
        return None, None
    plan = await session.get(SubscriptionPlan, subscription.plan_id)
    return subscription, plan


async def plan_for_code(session: AsyncSession, code: str) -> SubscriptionPlan | None:
    result = await session.execute(
        select(SubscriptionPlan).where(SubscriptionPlan.code == code)
    )
    return result.scalar_one_or_none()
