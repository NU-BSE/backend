from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from jwt import InvalidTokenError

from app.core.config import Settings
from app.core.errors import ApiError

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"

_ALGORITHM = "HS256"


@dataclass(frozen=True)
class TokenSubject:
    user_id: str
    email: str
    token_type: str


def _encode(settings: Settings, payload: dict[str, object]) -> str:
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def _base_claims(settings: Settings, user_id: str, email: str) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "sub": user_id,
        "email": email,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
    }


def create_access_token(settings: Settings, user_id: str, email: str) -> str:
    claims = _base_claims(settings, user_id, email)
    claims["type"] = TOKEN_TYPE_ACCESS
    claims["exp"] = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_ttl_minutes
    )
    return _encode(settings, claims)


def create_refresh_token(settings: Settings, user_id: str, email: str) -> str:
    claims = _base_claims(settings, user_id, email)
    claims["type"] = TOKEN_TYPE_REFRESH
    claims["exp"] = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_ttl_days)
    return _encode(settings, claims)


def decode_token(settings: Settings, token: str, expected_type: str) -> TokenSubject:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[_ALGORITHM],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["sub", "exp", "iat", "type"]},
        )
    except InvalidTokenError as exc:
        raise ApiError(401, "UNAUTHORIZED", "Token is invalid or expired") from exc

    user_id = payload.get("sub")
    token_type = payload.get("type")
    email = payload.get("email")
    if not isinstance(user_id, str) or not user_id:
        raise ApiError(401, "UNAUTHORIZED", "Token has no subject")
    if token_type != expected_type:
        raise ApiError(401, "UNAUTHORIZED", f"Expected a {expected_type} token")
    return TokenSubject(
        user_id=user_id,
        email=email if isinstance(email, str) else "",
        token_type=token_type,
    )
