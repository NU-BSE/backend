"""Email-code challenges: ephemeral, hashed, single-use.

Nothing here touches Postgres. Challenges live in the TTL store for
`email_code_ttl_seconds` and vanish, which keeps the persistence scope at
"users + subscriptions only."
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

import orjson

from app.core.config import Settings
from app.core.errors import ApiError
from app.kv.store import TTLStore

CHALLENGE_PREFIX = "auth:challenge:"
COOLDOWN_PREFIX = "auth:cooldown:"
RL_EMAIL_PREFIX = "auth:rl:email:"
RL_IP_PREFIX = "auth:rl:ip:"

HOUR_SECONDS = 3600


@dataclass(frozen=True)
class Challenge:
    challenge_id: str
    email: str
    name: str | None
    purpose: str


@dataclass(frozen=True)
class RequestCodeResult:
    challenge_id: str
    code: str
    expires_in_seconds: int
    retry_after_seconds: int


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


class EmailCodeService:
    def __init__(self, store: TTLStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings

    async def request_code(
        self,
        *,
        email: str,
        name: str | None,
        purpose: str,
        client_ip: str,
    ) -> RequestCodeResult:
        """Create a challenge and return it with the plaintext code to deliver."""
        retry_after = await self._cooldown_remaining(email)
        if retry_after > 0:
            raise ApiError(
                429,
                "RATE_LIMITED",
                "A verification code was sent recently. Please wait before requesting another.",
                retryable=True,
                extra={"retryAfterSeconds": retry_after},
            )

        email_count = await self._store.incr(
            f"{RL_EMAIL_PREFIX}{email}", HOUR_SECONDS
        )
        if email_count > self._settings.email_code_max_per_email_per_hour:
            raise ApiError(
                429,
                "RATE_LIMITED",
                "Too many verification codes requested for this email. Try again later.",
                retryable=True,
                extra={"retryAfterSeconds": HOUR_SECONDS},
            )

        ip_count = await self._store.incr(f"{RL_IP_PREFIX}{client_ip}", HOUR_SECONDS)
        if ip_count > self._settings.email_code_max_per_ip_per_hour:
            raise ApiError(
                429,
                "RATE_LIMITED",
                "Too many verification codes requested from this network. Try again later.",
                retryable=True,
                extra={"retryAfterSeconds": HOUR_SECONDS},
            )

        challenge_id = secrets.token_hex(16)
        code = f"{secrets.randbelow(1_000_000):06d}"
        payload = orjson.dumps(
            {
                "email": email,
                "name": name,
                "purpose": purpose,
                "code_hash": _hash_code(code),
                "attempts": 0,
            }
        ).decode()
        ttl = self._settings.email_code_ttl_seconds
        await self._store.set(f"{CHALLENGE_PREFIX}{challenge_id}", payload, ttl)
        await self._store.set(
            f"{COOLDOWN_PREFIX}{email}",
            "1",
            self._settings.email_code_resend_cooldown_seconds,
        )
        return RequestCodeResult(
            challenge_id=challenge_id,
            code=code,
            expires_in_seconds=ttl,
            retry_after_seconds=self._settings.email_code_resend_cooldown_seconds,
        )

    async def _cooldown_remaining(self, email: str) -> int:
        ttl = await self._store.ttl(f"{COOLDOWN_PREFIX}{email}")
        return max(ttl, 0)

    async def verify_code(self, *, challenge_id: str, email: str, code: str) -> Challenge:
        """Consume a challenge. Timing-safe compare; single-use; attempt-capped."""
        key = f"{CHALLENGE_PREFIX}{challenge_id}"
        raw = await self._store.get(key)
        if raw is None:
            raise ApiError(
                400,
                "CHALLENGE_EXPIRED",
                "The verification code is invalid or expired.",
                retryable=True,
            )

        data: dict = orjson.loads(raw)
        attempts = int(data.get("attempts", 0))
        if attempts >= self._settings.email_code_max_verify_attempts:
            await self._store.delete(key)
            raise ApiError(
                429,
                "RATE_LIMITED",
                "Too many incorrect codes. Request a new one.",
                retryable=True,
            )

        expected = str(data.get("code_hash", ""))
        if not hmac.compare_digest(expected, _hash_code(code)) or data.get("email") != email:
            data["attempts"] = attempts + 1
            remaining = await self._store.ttl(key)
            await self._store.set(key, orjson.dumps(data).decode(), max(remaining, 1))
            raise ApiError(400, "INVALID_CODE", "The verification code is invalid or expired.")

        # Single-use: consume before returning.
        await self._store.delete(key)
        return Challenge(
            challenge_id=challenge_id,
            email=str(data["email"]),
            name=data.get("name"),
            purpose=str(data.get("purpose", "login")),
        )
