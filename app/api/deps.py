from collections.abc import AsyncIterator

import httpx
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.security import TOKEN_TYPE_ACCESS, decode_token
from app.db.models import User
from app.kv.store import TTLStore
from app.services.email_codes import EmailCodeService
from app.services.email_sender import EmailSender
from app.services.usage import UsageMeter

bearer_scheme = HTTPBearer(auto_error=False)


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_store(request: Request) -> TTLStore:
    return request.app.state.store


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


def get_email_code_service(request: Request) -> EmailCodeService:
    return request.app.state.email_code_service


def get_email_sender(request: Request) -> EmailSender:
    return request.app.state.email_sender


def get_usage_meter(request: Request) -> UsageMeter:
    return request.app.state.usage_meter


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User:
    settings: Settings = request.app.state.settings
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "Bearer token is required")

    subject = decode_token(settings, credentials.credentials, TOKEN_TYPE_ACCESS)

    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        user = await session.get(User, subject.user_id)
    if user is None:
        raise ApiError(401, "UNAUTHORIZED", "User no longer exists")
    return user


async def require_admin(
    request: Request,
    user: User = Depends(get_current_user),
) -> User:
    settings: Settings = request.app.state.settings
    email = (user.email or "").lower()
    if not settings.admin_email_set or email not in settings.admin_email_set:
        raise ApiError(403, "FORBIDDEN", "Admin access required")
    return user
