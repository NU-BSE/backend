from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict] = {Decimal: Numeric(12, 2)}


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str | None] = mapped_column(String, unique=True)
    name: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"

    plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    code: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String, default="USD")
    interval: Mapped[str] = mapped_column("interval", String)
    interval_count: Mapped[int] = mapped_column(Integer, default=1)
    trial_days: Mapped[int] = mapped_column(Integer, default=0)
    agent_access: Mapped[bool] = mapped_column(Boolean, default=False)
    cloud_agent_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    max_agent_messages_per_day: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("provider", "provider_subscription_id"),)

    subscription_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[str] = mapped_column(String, ForeignKey("subscription_plans.plan_id"))
    status: Mapped[str] = mapped_column(String)
    provider: Mapped[str | None] = mapped_column(String)
    provider_subscription_id: Mapped[str | None] = mapped_column(String)
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SubscriptionInvoice(Base):
    __tablename__ = "subscription_invoices"

    invoice_id: Mapped[str] = mapped_column(String, primary_key=True)
    subscription_id: Mapped[str] = mapped_column(
        String, ForeignKey("subscriptions.subscription_id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.user_id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    provider: Mapped[str | None] = mapped_column(String)
    provider_payment_id: Mapped[str | None] = mapped_column(String)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SubscriptionEvent(Base):
    __tablename__ = "subscription_events"

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    subscription_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("subscriptions.subscription_id", ondelete="SET NULL"), index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SubscriptionEntitlement(Base):
    __tablename__ = "subscription_entitlements"

    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True
    )
    entitlement: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, default="subscription")
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
