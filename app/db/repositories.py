import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Subscription, SubscriptionEntitlement, SubscriptionPlan, User

ACTIVE_SUBSCRIPTION_STATUSES = ("active", "trialing")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: str) -> User | None:
    return await session.get(User, user_id)


async def create_user(session: AsyncSession, email: str, name: str | None) -> User:
    user = User(user_id=new_id("usr"), email=email, name=name)
    session.add(user)
    await session.flush()
    return user


async def update_user_name(session: AsyncSession, user_id: str, name: str) -> None:
    await session.execute(
        update(User)
        .values(name=name, updated_at=datetime.now(timezone.utc))
        .where(User.user_id == user_id)
    )


async def list_active_plans(session: AsyncSession) -> list[SubscriptionPlan]:
    result = await session.execute(
        select(SubscriptionPlan).where(SubscriptionPlan.active.is_(True)).order_by(
            SubscriptionPlan.amount
        )
    )
    return list(result.scalars().all())


async def get_plan_by_code(session: AsyncSession, code: str) -> SubscriptionPlan | None:
    result = await session.execute(select(SubscriptionPlan).where(SubscriptionPlan.code == code))
    return result.scalar_one_or_none()


async def get_active_subscription(session: AsyncSession, user_id: str) -> Subscription | None:
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status.in_(ACTIVE_SUBSCRIPTION_STATUSES),
            Subscription.current_period_end > now,
        )
        .order_by(Subscription.current_period_end.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_subscription(
    session: AsyncSession,
    *,
    user_id: str,
    plan_id: str,
    status: str,
    provider: str,
    provider_subscription_id: str | None,
    period_start: datetime,
    period_end: datetime,
) -> Subscription:
    subscription = Subscription(
        subscription_id=new_id("sub"),
        user_id=user_id,
        plan_id=plan_id,
        status=status,
        provider=provider,
        provider_subscription_id=provider_subscription_id,
        current_period_start=period_start,
        current_period_end=period_end,
    )
    session.add(subscription)
    await session.flush()
    return subscription


async def cancel_subscription(session: AsyncSession, subscription: Subscription) -> None:
    now = datetime.now(timezone.utc)
    subscription.status = "canceled"
    subscription.canceled_at = now
    subscription.ended_at = now
    subscription.updated_at = now
    await session.flush()


async def upsert_entitlements(
    session: AsyncSession,
    user_id: str,
    entitlements: dict[str, datetime | None],
    source: str,
) -> None:
    for name, expires_at in entitlements.items():
        existing = await session.get(SubscriptionEntitlement, (user_id, name))
        if existing is None:
            session.add(
                SubscriptionEntitlement(
                    user_id=user_id,
                    entitlement=name,
                    source=source,
                    expires_at=expires_at,
                )
            )
        else:
            existing.source = source
            existing.granted_at = datetime.now(timezone.utc)
            existing.expires_at = expires_at
    await session.flush()


async def clear_entitlements(session: AsyncSession, user_id: str) -> None:
    existing = await session.execute(
        select(SubscriptionEntitlement).where(SubscriptionEntitlement.user_id == user_id)
    )
    for row in existing.scalars().all():
        await session.delete(row)
    await session.flush()


async def get_active_entitlements(session: AsyncSession, user_id: str) -> set[str]:
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(SubscriptionEntitlement.entitlement).where(
            SubscriptionEntitlement.user_id == user_id,
            (SubscriptionEntitlement.expires_at.is_(None))
            | (SubscriptionEntitlement.expires_at > now),
        )
    )
    return set(result.scalars().all())


def lifetime_period_end() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=365 * 100)
