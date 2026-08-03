"""Entitlement resolution.

The subscription rows are the source of truth; `subscription_entitlements`
is the materialized lookup the agent gate checks, so expiry and grants both
collapse into one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repositories as repo
from app.db.models import Subscription, SubscriptionPlan

ENTITLEMENT_AGENT_ACCESS = "agent_access"
ENTITLEMENT_CLOUD_AGENT = "cloud_agent_allowed"


@dataclass(frozen=True)
class EffectiveEntitlements:
    agent_access: bool
    cloud_agent_allowed: bool
    max_agent_messages_per_day: int | None
    plan_code: str | None
    subscription_status: str | None
    current_period_end: datetime | None


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


async def sync_for_user(session: AsyncSession, user_id: str) -> EffectiveEntitlements:
    """Resolve (and re-materialize) the effective rights for a user."""
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
