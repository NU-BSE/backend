from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError

from app.core.config import Settings, get_settings
from app.core.errors import ApiError

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    user_id: str


def create_access_token(user_id: str, settings: Settings, ttl_minutes: int = 60) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "iss": settings.auth_jwt_issuer,
        "aud": settings.auth_jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes),
    }
    return jwt.encode(payload, settings.auth_jwt_secret, algorithm="HS256")


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "Bearer token is required")

    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.auth_jwt_secret,
            algorithms=["HS256"],
            issuer=settings.auth_jwt_issuer,
            audience=settings.auth_jwt_audience,
            options={"require": ["sub", "iss", "aud", "iat", "exp"]},
        )
    except InvalidTokenError as exc:
        raise ApiError(401, "UNAUTHORIZED", "Invalid access token") from exc

    user_id = payload.get("sub")
    if not isinstance(user_id, str) or not user_id.strip():
        raise ApiError(401, "UNAUTHORIZED", "Access token has no user subject")
    return CurrentUser(user_id=user_id)
