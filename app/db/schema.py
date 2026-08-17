import logging
import re

import anyio
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.db.models import Base, SubscriptionPlan

logger = logging.getLogger("app.db")

# The Play product these plans are sold as. One subscription with two base
# plans, which is how Play models a monthly/annual choice — not two products.
PLAY_SUBSCRIPTION_PRODUCT_ID = "creepyim_pro"

# The base plan ids exactly as the Play Console generated them.
#
# Opaque ids, not descriptions. Readable ones were invented here and in the
# app, and Play answered "no active offer for creepyim-pro-annual" for a plan
# that was published and active the whole time — the console had named it
# plan-2. Anything that has to match Play must be copied from Play.
PLAY_BASE_PLAN_MONTHLY = "plan-1"
PLAY_BASE_PLAN_ANNUAL = "plan-2"

# Play's base plan id is the only thing that identifies which plan was bought,
# so it is the join between Google's world and ours. The client sends a token
# and nothing else; this map is applied to what Google says the token bought.
PLAY_BASE_PLAN_TO_CODE: dict[str, str] = {
    PLAY_BASE_PLAN_MONTHLY: "pro",
    PLAY_BASE_PLAN_ANNUAL: "pro_annual",
}

# Prices and trial length track creepy.im. They were $9.99/month with no trial
# and no annual option, which contradicted both the site and the paywall the
# app ships — a user who read the site and then subscribed would have been
# charged from a catalogue nobody had seen.
#
# These are the prices we *display* before Play answers. Play's own localized
# price is authoritative at purchase time, and the amount recorded against a
# subscription comes from Play, not from here.
SEED_PLANS: list[dict[str, object]] = [
    {
        "plan_id": "plan_free",
        "code": "free",
        "name": "Free",
        "description": "On-device agent only. The cloud agent requires Pro.",
        "amount": "0",
        "currency": "USD",
        "interval": "lifetime",
        "interval_count": 1,
        "trial_days": 0,
        "agent_access": True,
        "cloud_agent_allowed": False,
        "max_agent_messages_per_day": None,
        "active": True,
    },
    {
        "plan_id": "plan_pro",
        "code": "pro",
        "name": "Creepy Pro (monthly)",
        "description": "Everything Creepy does, billed monthly.",
        "amount": "12.90",
        "currency": "USD",
        "interval": "month",
        "interval_count": 1,
        # No trial on monthly: the Play offer (trial-2) hangs off the annual
        # base plan only, so a trial advertised here would be one the store
        # never grants.
        "trial_days": 0,
        "agent_access": True,
        "cloud_agent_allowed": True,
        "max_agent_messages_per_day": 200,
        "active": True,
    },
    {
        "plan_id": "plan_pro_annual",
        "code": "pro_annual",
        "name": "Creepy Pro (annual)",
        "description": "Everything Creepy does, billed yearly. Two months free.",
        "amount": "118.80",
        "currency": "USD",
        "interval": "year",
        "interval_count": 1,
        "trial_days": 7,
        "agent_access": True,
        "cloud_agent_allowed": True,
        "max_agent_messages_per_day": 200,
        "active": True,
    },
]


def _split_statements(sql: str) -> list[str]:
    # Strip single-line comments (-- ...) entirely
    cleaned_sql = re.sub(r"--.*$", "", sql, flags=re.MULTILINE)
    
    statements: list[str] = []
    for raw in cleaned_sql.split(";"):
        stmt = raw.strip()
        if stmt:
            statements.append(stmt)
            
    return statements


async def apply_schema(engine: AsyncEngine, settings: Settings) -> None:
    """Idempotently apply the persisted schema.

    Postgres runs the checked-in SQL file (the same contract the frontend
    repo keeps in db/schema.sql); other dialects (tests) fall back to the
    SQLAlchemy metadata.
    """
    if not settings.apply_schema_on_startup:
        return

    if engine.dialect.name == "postgresql":
        sql = await anyio.Path(settings.schema_file).read_text(encoding="utf-8")
        async with engine.begin() as conn:
            for statement in _split_statements(sql):
                await conn.exec_driver_sql(statement)
        logger.info("schema applied from %s", settings.schema_file)
    else:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("schema applied from SQLAlchemy metadata")


# Fields the seed owns. `active` is excluded: an operator who deactivated a
# plan in the database meant it, and a redeploy must not quietly sell it again.
_SEEDED_FIELDS = (
    "name",
    "description",
    "amount",
    "currency",
    "interval",
    "interval_count",
    "trial_days",
    "agent_access",
    "cloud_agent_allowed",
    "max_agent_messages_per_day",
)


async def seed_plans(engine: AsyncEngine) -> None:
    """Upsert the plan catalogue (idempotent).

    This used to insert and never update, despite the docstring. A price
    change therefore reached new deployments only — every existing database
    kept whatever it was first seeded with, and the catalogue the API served
    silently diverged from the catalogue in the repository. Existing rows are
    now reconciled to the seed.
    """
    async with engine.begin() as conn:
        for plan in SEED_PLANS:
            code = str(plan["code"])
            existing = await conn.execute(
                select(SubscriptionPlan).where(SubscriptionPlan.code == code)
            )
            row = existing.mappings().first()
            if row is None:
                await conn.execute(insert(SubscriptionPlan).values(**plan))
                logger.info("seeded plan %s", code)
                continue

            changes = {
                field: plan[field]
                for field in _SEEDED_FIELDS
                # Numerics come back as Decimal and booleans as bool; compare
                # as text so "12.90" and Decimal("12.90") are not a difference.
                if str(plan[field]) != str(row[field])
            }
            if changes:
                await conn.execute(
                    update(SubscriptionPlan)
                    .where(SubscriptionPlan.code == code)
                    .values(**changes)
                )
                logger.info("updated plan %s: %s", code, ", ".join(sorted(changes)))