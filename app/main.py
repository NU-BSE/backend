import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis_async
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import admin, auth, chat, health, models, subscriptions, users
from app.core.config import Settings, get_settings
from app.core.errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from app.core.logging import RequestContextMiddleware, configure_logging
from app.db.schema import apply_schema, seed_plans
from app.db.session import create_engine_and_session_factory
from app.kv.store import MemoryTTLStore, RedisTTLStore, TTLStore
from app.services.email_codes import EmailCodeService
from app.services.email_sender import EmailSender
from app.services.model_catalog import ModelCatalog
from app.services.play_billing import PlayClient
from app.services.usage import UsageMeter

logger = logging.getLogger("app")


def _create_store(settings: Settings) -> tuple[TTLStore, redis_async.Redis | None]:
    if not settings.redis_url:
        logger.warning("REDIS_URL not set - using in-process TTL store (dev only)")
        return MemoryTTLStore(), None
    redis = redis_async.from_url(settings.redis_url, decode_responses=True)
    return RedisTTLStore(redis), redis


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging("DEBUG" if settings.app_debug else "INFO")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine, session_factory = create_engine_and_session_factory(settings.database_url)
        store, redis = _create_store(settings)
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=10.0),
            follow_redirects=False,
        )

        if redis is not None:
            await redis.ping()

        await apply_schema(engine, settings)
        await seed_plans(engine)

        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.store = store
        app.state.redis = redis
        app.state.http_client = http_client
        app.state.email_code_service = EmailCodeService(store, settings)
        app.state.email_sender = EmailSender(http_client, settings)
        app.state.usage_meter = UsageMeter(store)
        app.state.play_client = PlayClient(settings, http_client)
        app.state.model_catalog = ModelCatalog(settings)

        logger.info("%s started (env=%s)", settings.app_name, settings.app_env)
        try:
            yield
        finally:
            await http_client.aclose()
            await store.aclose()
            await engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )

    app.add_exception_handler(
        ApiError, api_error_handler  # type: ignore[arg-type]
    )
    app.add_exception_handler(
        RequestValidationError, validation_error_handler  # type: ignore[arg-type]
    )
    app.add_exception_handler(
        StarletteHTTPException, http_exception_handler  # type: ignore[arg-type]
    )
    app.add_exception_handler(Exception, unhandled_error_handler)

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list or ["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Request-ID"],
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(subscriptions.router)
    app.include_router(admin.router)
    app.include_router(chat.router)
    app.include_router(models.router)


    @app.get("/")
    async def root() -> dict[str, str]:
        return {"service": settings.app_name, "status": "ok"}

    return app


app = create_app()
