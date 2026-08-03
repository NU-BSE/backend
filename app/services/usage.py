"""Daily per-user agent usage metering (Redis INCR with a daily TTL)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.kv.store import TTLStore

USAGE_PREFIX = "usage:agent:"


def _day_key(user_id: str, now: datetime) -> str:
    return f"{USAGE_PREFIX}{user_id}:{now.strftime('%Y-%m-%d')}"


def _seconds_until_next_utc_day(now: datetime) -> int:
    next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((next_day - now).total_seconds()), 1)


@dataclass(frozen=True)
class UsageCheck:
    allowed: bool
    used: int
    limit: int | None
    retry_after_seconds: int


class UsageMeter:
    def __init__(self, store: TTLStore) -> None:
        self._store = store

    async def check_and_increment(
        self, user_id: str, limit: int | None, now: datetime | None = None
    ) -> UsageCheck:
        """Atomically count this message; reject when it would exceed the cap."""
        moment = now or datetime.now(timezone.utc)
        key = _day_key(user_id, moment)
        ttl = _seconds_until_next_utc_day(moment) + 3600

        used = await self._store.incr(key, ttl)
        if limit is not None and used > limit:
            # Rejected requests must not consume quota.
            await self._store.decr(key)
            return UsageCheck(
                allowed=False,
                used=limit,
                limit=limit,
                retry_after_seconds=_seconds_until_next_utc_day(moment),
            )
        return UsageCheck(allowed=True, used=used, limit=limit, retry_after_seconds=0)

    async def current(self, user_id: str, now: datetime | None = None) -> int:
        moment = now or datetime.now(timezone.utc)
        raw = await self._store.get(_day_key(user_id, moment))
        return int(raw) if raw is not None else 0
