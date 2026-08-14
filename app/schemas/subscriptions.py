from datetime import datetime
from decimal import Decimal

from pydantic import Field

from app.schemas.common import CamelModel


class PlanResponse(CamelModel):
    plan_id: str
    code: str
    name: str
    description: str | None
    amount: Decimal
    currency: str
    interval: str
    interval_count: int
    trial_days: int
    agent_access: bool
    cloud_agent_allowed: bool
    max_agent_messages_per_day: int | None


class PlansResponse(CamelModel):
    plans: list[PlanResponse]


class EntitlementsResponse(CamelModel):
    agent_access: bool
    cloud_agent_allowed: bool
    max_agent_messages_per_day: int | None
    plan_code: str | None
    subscription_status: str | None
    current_period_end: datetime | None


class SubscriptionResponse(CamelModel):
    subscription_id: str
    plan_id: str
    status: str
    provider: str | None
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool


class MySubscriptionResponse(CamelModel):
    subscription: SubscriptionResponse | None
    entitlements: EntitlementsResponse


class GrantRequest(CamelModel):
    user_id: str
    plan_code: str
    days: int = Field(ge=1, le=3650)


class GrantResponse(CamelModel):
    subscription: SubscriptionResponse
    entitlements: EntitlementsResponse


class PlayVerifyRequest(CamelModel):
    """What the app knows after Play reports a successful purchase.

    Only `purchase_token` is trusted. The other two are carried for logging and
    for the acknowledge call, and are checked against what Google reports
    rather than believed: a client that could name its own product and base
    plan could name the cheapest one and be given the dearest.
    """

    purchase_token: str = Field(min_length=1, max_length=2048)
    product_id: str | None = Field(default=None, max_length=255)
    base_plan_id: str | None = Field(default=None, max_length=255)


class PlayVerifyResponse(CamelModel):
    subscription: SubscriptionResponse
    entitlements: EntitlementsResponse
