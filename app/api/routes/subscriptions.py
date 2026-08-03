from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.db import repositories as repo
from app.db.models import Subscription, User
from app.schemas.subscriptions import (
    EntitlementsResponse,
    MySubscriptionResponse,
    PlanResponse,
    PlansResponse,
    SubscriptionResponse,
)
from app.services import entitlements

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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MySubscriptionResponse:
    effective = await entitlements.sync_for_user(db, user.user_id)
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
        ),
    )
