import json
import secrets
from dataclasses import asdict, dataclass
from time import time_ns

from redis.asyncio import Redis

from app.core.errors import ApiError


@dataclass(frozen=True)
class StoredNonce:
    nonce: str
    server_issued_at: int
    client_ts: int
    client_ip: str
    user_agent: str
    user_id: str
    expires_at: int


_CONSUME_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then
  return nil
end
local record = cjson.decode(raw)
if record['user_id'] ~= ARGV[1] then
  return '__USER_MISMATCH__'
end
redis.call('DEL', KEYS[1])
return raw
"""


class RedisNonceStore:
    def __init__(self, redis: Redis, ttl_ms: int) -> None:
        self.redis = redis
        self.ttl_ms = ttl_ms

    @staticmethod
    def _key(nonce: str) -> str:
        return f"attestation:nonce:{nonce}"

    async def issue(
        self,
        *,
        user_id: str,
        client_ts: int,
        client_ip: str,
        user_agent: str,
    ) -> StoredNonce:
        for _ in range(3):
            nonce = secrets.token_urlsafe(32)
            now_ms = time_ns() // 1_000_000
            record = StoredNonce(
                nonce=nonce,
                server_issued_at=now_ms,
                client_ts=client_ts,
                client_ip=client_ip,
                user_agent=user_agent,
                user_id=user_id,
                expires_at=now_ms + self.ttl_ms,
            )
            stored = await self.redis.set(
                self._key(nonce),
                json.dumps(asdict(record), separators=(",", ":")),
                nx=True,
                px=self.ttl_ms,
            )
            if stored:
                return record
        raise ApiError(503, "NONCE_STORE_UNAVAILABLE", "Could not allocate nonce", retryable=True)

    async def consume(self, nonce: str, user_id: str) -> StoredNonce:
        raw = await self.redis.eval(_CONSUME_LUA, 1, self._key(nonce), user_id)
        if raw is None:
            raise ApiError(
                400,
                "NONCE_REJECTED",
                "Nonce expired, missing, or already used",
                retryable=True,
            )
        if raw == "__USER_MISMATCH__":
            raise ApiError(403, "NONCE_REJECTED", "Nonce belongs to another user")
        data = json.loads(raw)
        return StoredNonce(**data)
