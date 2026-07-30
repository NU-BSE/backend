from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis_async
from fastapi import FastAPI
from fastapi.responses import ORJSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import dev, health, jwks, nonce, verify
from app.core.config import get_settings
from app.core.errors import ApiError, api_error_handler
from app.db.session import create_engine_and_session_factory
from app.services.jit import LocalFileEd25519Signer
from app.services.nonce import RedisNonceStore
from app.services.play_integrity import GooglePlayIntegrityVerifier
from app.services.risk import RedisRiskProvider, StaticAsnLookup
from app.services.play_integrity import create_play_integrity_verifier

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine, session_factory = create_engine_and_session_factory(settings.database_url)
    redis = redis_async.from_url(settings.redis_url, decode_responses=True)
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))

    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.redis = redis
    app.state.http_client = http_client
    app.state.nonce_store = RedisNonceStore(redis, settings.nonce_ttl_ms)
    app.state.risk_provider = RedisRiskProvider(redis, settings)
    app.state.asn_lookup = StaticAsnLookup()
    app.state.play_integrity_verifier = (
        create_play_integrity_verifier(settings)
    )
    app.state.jit_signer = LocalFileEd25519Signer.from_file(
        settings.jit_private_key_file,
        settings,
    )

    await redis.ping()
    try:
        yield
    finally:
        await http_client.aclose()
        await redis.aclose()
        await engine.dispose()


app = FastAPI(
    root_path="/api",
    title=settings.app_name,
    version="0.1.0",
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)
app.add_exception_handler(ApiError, api_error_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(health.router)
app.include_router(dev.router)
app.include_router(nonce.router)
app.include_router(verify.router)
app.include_router(jwks.router)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": settings.app_name, "status": "ok"}
