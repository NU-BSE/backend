import logging
from unittest.mock import AsyncMock

from app.core.config import Settings
from app.kv.store import MemoryTTLStore
from app.llm.expert_budget import ExpertBudgetService
from app.llm.gateway import agent_step
from app.llm.schemas import (
    AgentStepRequest,
    ContextSignals,
    RoutingContext,
    StruggleSignals,
    WeakExecutionSignals,
)


def _make_request(
    requested_tier: str = "fast",
    reasoning_score: int = 0,
    messages: list[dict] | None = None,
) -> AgentStepRequest:
    return AgentStepRequest(
        request_id="req-1",
        run_id="run-1",
        routing=RoutingContext(
            requested_tier=requested_tier,  # type: ignore[arg-type]
            reasoning_score=reasoning_score,
            hard_reasoning_signals=[],
            weak_signals=WeakExecutionSignals(),
            struggle=StruggleSignals(),
            context=ContextSignals(),
        ),
        messages=messages or [{"role": "user", "content": "hello"}],
    )


class TestPrivacyInGatewayLogs:

    async def test_logs_do_not_contain_openrouter_key(self, settings: Settings, caplog):
        settings.openrouter_api_key = "sk-or-secret-key-do-not-log"
        settings.llm_model_fast = "test/fast"
        settings.llm_model_normal = "test/normal"
        settings.llm_model_expert = "test/expert"

        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
        )

        budget = ExpertBudgetService(MemoryTTLStore(), settings)
        req = _make_request()

        with caplog.at_level(logging.INFO, logger="app.llm"):
            await agent_step(
                client=mock_client,  # type: ignore[arg-type]
                settings=settings,
                budget=budget,
                request=req,
                user_id="u-1",
            )

        combined = " ".join(caplog.messages)
        assert "sk-or-secret" not in combined
        assert "OPENROUTER_API_KEY" not in combined

    async def test_logs_do_not_contain_oauth_token(self, settings: Settings, caplog):
        settings.llm_model_fast = "test/fast"
        settings.llm_model_normal = "test/normal"
        settings.llm_model_expert = "test/expert"

        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
        )

        budget = ExpertBudgetService(MemoryTTLStore(), settings)
        req = _make_request(
            messages=[{"role": "user", "content": "my oauth token is ya29.secret123"}],
        )

        with caplog.at_level(logging.INFO, logger="app.llm"):
            await agent_step(
                client=mock_client,  # type: ignore[arg-type]
                settings=settings,
                budget=budget,
                request=req,
                user_id="u-1",
            )

        combined = " ".join(caplog.messages)
        assert "ya29.secret123" not in combined

    async def test_expert_budget_logs_do_not_leak_pii(
        self, settings: Settings, caplog
    ):
        settings.llm_expert_daily_user_limit = 5

        from app.llm.expert_budget import ExpertBudgetService
        store = MemoryTTLStore()
        budget = ExpertBudgetService(store, settings)

        with caplog.at_level(logging.INFO, logger="app.llm.expert_budget"):
            await budget.record_expert_use(
                user_id="user-abc-123", run_id="run-xyz",
            )

        combined = " ".join(caplog.messages)

        assert "Bearer" not in combined
        assert "password" not in combined.lower()

    async def test_routing_reason_present_in_response(
        self, settings: Settings
    ):
        """Verify routing reason is included in the response."""
        settings.llm_model_fast = "test/fast"
        settings.llm_model_normal = "test/normal"
        settings.llm_model_expert = "test/expert"

        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
        )

        budget = ExpertBudgetService(MemoryTTLStore(), settings)
        req = _make_request(requested_tier="fast", reasoning_score=0)

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=budget,
            request=req,
            user_id="u-1",
        )

        assert resp.routing_reason is not None
        assert resp.routing_reason == "default_fast"
        assert resp.requested_model_tier == "fast"
        assert resp.effective_model_tier == "fast"
        assert resp.request_id == "req-1"
        assert resp.run_id == "run-1"

    async def test_result_in_response(self, settings: Settings):
        """Verify result format in response."""
        settings.llm_model_fast = "test/fast"

        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"hi"}}]}'
        )

        budget = ExpertBudgetService(MemoryTTLStore(), settings)
        req = _make_request()

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=budget,
            request=req,
            user_id="u-1",
        )

        assert resp.result.kind == "final"
        assert resp.result.text == "hi"
