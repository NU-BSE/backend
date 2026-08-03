from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import User
from app.schemas.subscriptions import (
    EntitlementsResponse,
    GrantRequest,
    GrantResponse,
    SubscriptionResponse,
)
from app.services import entitlements

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/grant", response_model=GrantResponse)
async def grant(
    body: GrantRequest,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> GrantResponse:
    user = await repo.get_user_by_id(db, body.user_id)
    if user is None:
        raise ApiError(404, "USER_NOT_FOUND", "No such user")

    plan = await repo.get_plan_by_code(db, body.plan_code)
    if plan is None or not plan.active:
        raise ApiError(404, "PLAN_NOT_FOUND", f"No active plan with code {body.plan_code}")

    previous = await repo.get_active_subscription(db, user.user_id)
    if previous is not None:
        await repo.cancel_subscription(db, previous)

    now = datetime.now(timezone.utc)
    subscription = await repo.create_subscription(
        db,
        user_id=user.user_id,
        plan_id=plan.plan_id,
        status="active",
        provider="manual",
        provider_subscription_id=None,
        period_start=now,
        period_end=now + timedelta(days=body.days),
    )
    await entitlements.materialize(
        db,
        user_id=user.user_id,
        plan=plan,
        expires_at=subscription.current_period_end,
        source="grant",
    )

    return GrantResponse(
        subscription=SubscriptionResponse.model_validate(subscription, from_attributes=True),
        entitlements=EntitlementsResponse(
            agent_access=plan.agent_access,
            cloud_agent_allowed=plan.cloud_agent_allowed,
            max_agent_messages_per_day=plan.max_agent_messages_per_day,
            plan_code=plan.code,
            subscription_status=subscription.status,
            current_period_end=subscription.current_period_end,
        ),
    )
