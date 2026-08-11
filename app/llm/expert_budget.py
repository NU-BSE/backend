from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.core.config import Settings
from app.kv.store import TTLStore

logger = logging.getLogger("app.llm.expert_budget")

EXPERT_PREFIX = "expert:user:"
EXPERT_RUN_PREFIX = "expert:run:"


def _user_day_key(user_id: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{EXPERT_PREFIX}{user_id}:{today}"


def _run_key(user_id: str, run_id: str) -> str:
    return f"{EXPERT_RUN_PREFIX}{user_id}:{run_id}"


def _seconds_until_next_utc_day() -> int:
    now = datetime.now(timezone.utc)
    next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((next_day - now).total_seconds()), 1)


class ExpertBudgetService:
    def __init__(self, store: TTLStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings

    async def can_use_expert(self, *, user_id: str, run_id: str) -> bool:
        daily_key = _user_day_key(user_id)
        daily_limit = self._settings.llm_expert_daily_user_limit
        daily_used = int(await self._store.get(daily_key) or "0")
        if daily_used >= daily_limit:
            return False

        run_key = _run_key(user_id, run_id)
        run_cap = self._settings.llm_expert_max_calls_per_run
        run_used = int(await self._store.get(run_key) or "0")
        if run_used >= run_cap:
            return False

        return True

    async def reserve_expert(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> bool:
        """Atomically check + increment daily and per-run counters.

        Returns True if the reservation succeeded (within limits), False otherwise.
        """
        daily_key = _user_day_key(user_id)
        daily_limit = self._settings.llm_expert_daily_user_limit
        daily_ttl = _seconds_until_next_utc_day() + 3600

        # Atomically increment the daily counter.
        new_daily = await self._store.incr(daily_key, daily_ttl)
        if new_daily > daily_limit:
            # Rollback — the request exceeded the limit.
            await self._store.decr(daily_key)
            return False

        run_key = _run_key(user_id, run_id)
        run_cap = self._settings.llm_expert_max_calls_per_run
        run_ttl = 86400

        new_run = await self._store.incr(run_key, run_ttl)
        if new_run > run_cap:
            await self._store.decr(run_key)
            await self._store.decr(daily_key)
            return False

        return True

    async def record_expert_use(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> None:
        """Record expert usage without limit checking (counters already reserved)."""
        daily_key = _user_day_key(user_id)
        daily_ttl = _seconds_until_next_utc_day() + 3600
        await self._store.incr(daily_key, daily_ttl)

        run_key = _run_key(user_id, run_id)
        run_ttl = 86400
        await self._store.incr(run_key, run_ttl)

        logger.info(
            "expert use recorded user=%s run=%s",
            user_id,
            run_id,
        )
