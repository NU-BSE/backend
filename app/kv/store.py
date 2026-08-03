"""Ephemeral key-value store with TTL semantics.

Backs email-code challenges, rate limits and daily usage counters. Redis in
any real deployment; an in-process fallback keeps local dev and tests free of
infrastructure. Nothing here is durable by design.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis


class TTLStore:
    async def get(self, key: str) -> str | None:
        raise NotImplementedError

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        raise NotImplementedError

    async def incr(self, key: str, ttl_seconds: int) -> int:
        """Atomic increment that sets a TTL on first use."""
        raise NotImplementedError

    async def decr(self, key: str) -> int:
        """Atomic decrement."""
        raise NotImplementedError

    async def ttl(self, key: str) -> int:
        """Remaining TTL in seconds; -2 if the key does not exist."""
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


class MemoryTTLStore(TTLStore):
    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float]] = {}
        self._counters: dict[str, tuple[int, float]] = {}

    def _alive(self, expires_at: float) -> bool:
        return expires_at > time.monotonic()

    async def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is not None and self._alive(item[1]):
            return item[0]
        counter = self._counters.get(key)
        if counter is not None and self._alive(counter[1]):
            return str(counter[0])
        return None

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._data[key] = (value, time.monotonic() + ttl_seconds)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._counters.pop(key, None)

    async def incr(self, key: str, ttl_seconds: int) -> int:
        item = self._counters.get(key)
        if item is None or not self._alive(item[1]):
            self._counters[key] = (1, time.monotonic() + ttl_seconds)
            return 1
        value = item[0] + 1
        self._counters[key] = (value, item[1])
        return value

    async def decr(self, key: str) -> int:
        item = self._counters.get(key)
        if item is None or not self._alive(item[1]):
            return 0
        value = max(item[0] - 1, 0)
        self._counters[key] = (value, item[1])
        return value

    async def ttl(self, key: str) -> int:
        for store in (self._data, self._counters):
            item = store.get(key)
            if item is not None:
                expires_at = item[1]
                if self._alive(expires_at):
                    return max(int(expires_at - time.monotonic()), 0)
        return -2


class RedisTTLStore(TTLStore):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def get(self, key: str) -> str | None:
        value = await self._redis.get(key)
        return value if isinstance(value, str) else None

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        await self._redis.set(key, value, ex=ttl_seconds)

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def incr(self, key: str, ttl_seconds: int) -> int:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl_seconds, nx=True)
            results = await pipe.execute()
        return int(results[0])

    async def decr(self, key: str) -> int:
        return int(await self._redis.decr(key))

    async def ttl(self, key: str) -> int:
        return int(await self._redis.ttl(key))

    async def aclose(self) -> None:
        await self._redis.aclose()


StoreFactory = Callable[[], Awaitable[TTLStore]]
