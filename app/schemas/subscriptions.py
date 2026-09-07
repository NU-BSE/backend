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
    # Whether this account has to buy anything to use the product. The app
    # skips the paywall when this is false, which keeps the list of exempt
    # accounts entirely server-side — the client never learns who is exempt or
    # why, only that this caller is.
    #
    # Defaulted to True so that an older server, or any response that omits it,
    # is read as "payment required". A bypass must never be the fallback.
    subscription_required: bool = True


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


class BetaGrantRequest(CamelModel):
    """Custdev / beta override: this user passes onboarding without a paywall.

    A grant is a server-side decision recorded in the entitlement table, so the
    app never has to know which accounts are special — it only reads
    `subscriptionRequired` and behaves accordingly.
    """

    user_id: str


class BetaGrantResponse(CamelModel):
    granted: bool
    entitlements: set[str]


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
