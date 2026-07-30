from collections.abc import AsyncIterator

import httpx
from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.jit import LocalFileEd25519Signer
from app.services.nonce import RedisNonceStore
from app.services.play_integrity import GooglePlayIntegrityVerifier
from app.services.risk import RedisRiskProvider, StaticAsnLookup


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


def get_nonce_store(request: Request) -> RedisNonceStore:
    return request.app.state.nonce_store


def get_risk_provider(request: Request) -> RedisRiskProvider:
    return request.app.state.risk_provider


def get_asn_lookup(request: Request) -> StaticAsnLookup:
    return request.app.state.asn_lookup


def get_play_integrity_verifier(request: Request) -> GooglePlayIntegrityVerifier:
    return request.app.state.play_integrity_verifier


def get_jit_signer(request: Request) -> LocalFileEd25519Signer:
    return request.app.state.jit_signer


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client
