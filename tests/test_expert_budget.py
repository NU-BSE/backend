import pytest

from app.core.config import Settings
from app.kv.store import MemoryTTLStore
from app.llm.expert_budget import ExpertBudgetService


def _make_settings(**kwargs) -> Settings:
    defaults = {
        "jwt_secret": "test-secret-" + "x" * 40,
        "brevo_api_key": "test",
        "email_from": "test@test.com",
        "llm_expert_daily_user_limit": 20,
        "llm_expert_max_calls_per_run": 3,
    }
    return Settings(_env_file=None, **defaults, **kwargs)  # type: ignore[arg-type]


class TestExpertBudgetService:

    @pytest.fixture
    def store(self):
        return MemoryTTLStore()

    @pytest.fixture
    def settings(self):
        return _make_settings()

    @pytest.fixture
    def budget(self, store, settings):
        return ExpertBudgetService(store, settings)

    async def test_can_use_expert_within_limits(self, budget):
        assert await budget.can_use_expert(user_id="u-1", run_id="r-1") is True

    async def test_daily_limit_exhausted(self, store, settings):
        settings.llm_expert_daily_user_limit = 2
        budget = ExpertBudgetService(store, settings)

        assert await budget.reserve_expert(user_id="u-1", run_id="r-1") is True
        assert await budget.reserve_expert(user_id="u-1", run_id="r-2") is True
        assert await budget.reserve_expert(user_id="u-1", run_id="r-3") is False

    async def test_per_run_limit_exhausted(self, store, settings):
        settings.llm_expert_max_calls_per_run = 2
        budget = ExpertBudgetService(store, settings)

        assert await budget.reserve_expert(user_id="u-1", run_id="r-1") is True
        assert await budget.reserve_expert(user_id="u-1", run_id="r-1") is True
        assert await budget.reserve_expert(user_id="u-1", run_id="r-1") is False

    async def test_different_runs_have_separate_limits(self, store, settings):
        settings.llm_expert_max_calls_per_run = 1
        budget = ExpertBudgetService(store, settings)

        assert await budget.reserve_expert(user_id="u-1", run_id="r-1") is True
        assert await budget.reserve_expert(user_id="u-1", run_id="r-1") is False
        assert await budget.reserve_expert(user_id="u-1", run_id="r-2") is True

    async def test_zero_limit_denies_all(self, store, settings):
        settings.llm_expert_daily_user_limit = 0
        budget = ExpertBudgetService(store, settings)
        assert await budget.reserve_expert(user_id="u-1", run_id="r-1") is False

    async def test_reserve_is_atomic(self, store, settings):
        settings.llm_expert_daily_user_limit = 5
        budget = ExpertBudgetService(store, settings)

        for _i in range(5):
            assert await budget.reserve_expert(user_id="u-1", run_id=f"r-{_i}")

        assert await budget.reserve_expert(user_id="u-1", run_id="r-over") is False

    async def test_record_expert_use(self, budget):
        await budget.record_expert_use(user_id="u-1", run_id="r-1")
        assert await budget.can_use_expert(user_id="u-1", run_id="r-1") is True

    async def test_reserve_rolls_back_daily_on_run_overflow(self, store, settings):
        settings.llm_expert_daily_user_limit = 10
        settings.llm_expert_max_calls_per_run = 1
        budget = ExpertBudgetService(store, settings)

        # Use the one allowed call in this run.
        assert await budget.reserve_expert(user_id="u-1", run_id="r-1") is True
        # Next call to same run must overflow the per-run cap.
        assert await budget.reserve_expert(user_id="u-1", run_id="r-1") is False
        # Daily counter should be rolled back — only 1 used total, not 2.
        assert await budget.reserve_expert(user_id="u-1", run_id="r-2") is True
