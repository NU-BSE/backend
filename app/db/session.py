from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


def create_engine_and_session_factory(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    kwargs: dict = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        # Single shared connection keeps in-memory DBs alive across sessions
        # and avoids thread-affinity issues with aiosqlite.
        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_async_engine(database_url, **kwargs)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory
