from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.config import Settings
from app.kv.store import TTLStore

logger = logging.getLogger("app.llm.expert_budget")

EXPERT_PREFIX = "expert:user:"
EXPERT_RUN_PREFIX = "expert:run:"


def _user_day_key(user_id: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{EXPERT_PREFIX}{user_id}:{today}"


def _run_key(run_id: str) -> str:
    return f"{EXPERT_RUN_PREFIX}{run_id}"


def _seconds_until_next_utc_day() -> int:
    now = datetime.now(timezone.utc)
    from datetime import timedelta

    next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((next_day - now).total_seconds()), 1)


class ExpertBudgetService:
    def __init__(self, store: TTLStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings

    async def can_use_expert(self, *, user_id: str, run_id: str) -> bool:
        daily = await self._check_daily_limit(user_id)
        if not daily:
            return False

        per_run = await self._check_run_limit(user_id, run_id)
        if not per_run:
            return False

        return True

    async def _check_daily_limit(self, user_id: str) -> bool:
        limit = self._settings.llm_expert_daily_user_limit
        key = _user_day_key(user_id)
        raw = await self._store.get(key)
        current = int(raw) if raw is not None else 0
        return current < limit

    async def _check_run_limit(self, user_id: str, run_id: str) -> bool:
        cap = self._settings.llm_expert_max_calls_per_run
        key = _run_key(run_id)
        raw = await self._store.get(key)
        current = int(raw) if raw is not None else 0
        return current < cap

    async def record_expert_use(
        self,
        *,
        user_id: str,
        run_id: str,
        estimated_cost_usd: float | None = None,
    ) -> None:
        daily_key = _user_day_key(user_id)
        daily_ttl = _seconds_until_next_utc_day() + 3600
        await self._store.incr(daily_key, daily_ttl)

        run_key = _run_key(run_id)
        run_ttl = 86400
        await self._store.incr(run_key, run_ttl)

        logger.info(
            "expert use recorded user=%s run=%s cost=%s",
            user_id,
            run_id,
            estimated_cost_usd,
        )
