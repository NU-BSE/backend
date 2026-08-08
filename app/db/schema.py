import logging
import re
import anyio
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.db.models import Base, SubscriptionPlan

logger = logging.getLogger("app.db")

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
        "name": "Pro",
        "description": "Cloud agent with a generous daily allowance.",
        "amount": "9.99",
        "currency": "USD",
        "interval": "month",
        "interval_count": 1,
        "trial_days": 0,
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


async def seed_plans(engine: AsyncEngine) -> None:
    """Upsert the plan catalogue (idempotent)."""
    async with engine.begin() as conn:
        for plan in SEED_PLANS:
            code = str(plan["code"])
            existing = await conn.execute(
                select(SubscriptionPlan.plan_id).where(SubscriptionPlan.code == code)
            )
            row = existing.first()
            if row is None:
                await conn.execute(insert(SubscriptionPlan).values(**plan))
                logger.info("seeded plan %s", code)