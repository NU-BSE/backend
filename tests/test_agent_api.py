from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings
from app.llm.errors import RoutingValidationError
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
    model_tier: str = "fast",
    reasoning_score: int = 0,
    hard_signals: list[str] | None = None,
    failed_plans: int = 0,
    replans: int = 0,
    repeated_tool_pattern: bool = False,
    invalid_tool_calls: int = 0,
    step_count: int = 0,
    tool_calls: int = 0,
    connector_count: int = 0,
    escalation_count: int = 0,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> AgentStepRequest:
    return AgentStepRequest(
        request_id="req-1",
        run_id="run-1",
        model_tier=model_tier,  # type: ignore[arg-type]
        routing_context=RoutingContext(
            reasoning_score=reasoning_score,
            hard_reasoning_signals=hard_signals or [],
            weak_signals=WeakExecutionSignals(
                step_count=step_count,
                tool_calls=tool_calls,
                connector_count=connector_count,
            ),
            struggle=StruggleSignals(
                failed_plans=failed_plans,
                replans=replans,
                repeated_tool_pattern=repeated_tool_pattern,
                invalid_tool_calls=invalid_tool_calls,
            ),
            context=ContextSignals(),
            escalation_count=escalation_count,
        ),
        messages=messages or [{"role": "user", "content": "hello"}],
        tools=tools or [],
    )


def _make_completion(
    message: dict[str, Any], usage: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "choices": [{"message": message}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


@pytest.fixture
def budget(settings: Settings) -> ExpertBudgetService:
    from app.kv.store import MemoryTTLStore
    return ExpertBudgetService(MemoryTTLStore(), settings)


class TestAgentStepRouting:

    async def test_easy_long_task_stays_fast_or_normal(
        self, settings: Settings, budget: ExpertBudgetService
    ):
        """Long tool task without reasoning complexity stays fast/normal, never expert."""
        req = _make_request(
            model_tier="fast",
            reasoning_score=2,
            tool_calls=8,
            connector_count=2,
        )
        from unittest.mock import AsyncMock
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=budget,
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier in ("fast", "normal")
        assert resp.effective_model_tier != "expert"

    async def test_hard_short_task_goes_expert(
        self, settings: Settings, budget: ExpertBudgetService
    ):
        """High reasoning score + hard signals → expert."""
        req = _make_request(
            model_tier="normal",
            reasoning_score=12,
            hard_signals=["constraint_solving", "ranking_or_optimization"],
            tool_calls=2,
        )
        from unittest.mock import AsyncMock
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"solved"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=budget,
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "expert"
        assert resp.routing_reason == "hard_reasoning"

    async def test_client_asks_expert_without_evidence_downgraded(
        self, settings: Settings, budget: ExpertBudgetService
    ):
        """Buggy frontend asks for expert with no evidence → downgrade."""
        req = _make_request(
            model_tier="expert",
            reasoning_score=2,
        )
        from unittest.mock import AsyncMock
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"nope"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=budget,
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier != "expert"

    async def test_planner_stuck_triggers_expert(
        self, settings: Settings, budget: ExpertBudgetService
    ):
        """failed_plans >= 2 triggers emergency expert."""
        req = _make_request(
            model_tier="normal",
            reasoning_score=5,
            failed_plans=2,
        )
        from unittest.mock import AsyncMock
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"rescued"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=budget,
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "expert"
        assert resp.routing_reason == "planner_stuck"

    async def test_loop_detected_triggers_expert(
        self, settings: Settings, budget: ExpertBudgetService
    ):
        """repeated_tool_pattern triggers emergency expert."""
        req = _make_request(
            model_tier="normal",
            reasoning_score=3,
            repeated_tool_pattern=True,
        )
        from unittest.mock import AsyncMock
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"unlooped"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=budget,
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "expert"
        assert resp.routing_reason == "planner_stuck"

    async def test_expert_disabled_returns_normal(
        self, settings: Settings, budget: ExpertBudgetService
    ):
        settings.llm_allow_expert = False
        req = _make_request(
            model_tier="expert",
            reasoning_score=15,
            hard_signals=["constraint_solving"],
        )
        from unittest.mock import AsyncMock
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"normal"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=budget,
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "normal"
        assert resp.routing_reason == "expert_disabled"
        settings.llm_allow_expert = True

    async def test_expert_budget_denied_falls_back_to_normal(
        self, settings: Settings, budget: ExpertBudgetService
    ):
        settings.llm_expert_daily_user_limit = 0
        req = _make_request(
            model_tier="expert",
            reasoning_score=15,
            hard_signals=["cross_source_synthesis"],
        )
        from unittest.mock import AsyncMock
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"fallback"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=budget,
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "normal"
        assert resp.routing_reason == "expert_budget_unavailable"
        settings.llm_expert_daily_user_limit = 20

    async def test_replan_triggers_normal(
        self, settings: Settings, budget: ExpertBudgetService
    ):
        """A single replan should trigger normal tier."""
        req = _make_request(
            model_tier="fast",
            reasoning_score=2,
            replans=1,
        )
        from unittest.mock import AsyncMock
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"retrying"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=budget,
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "normal"
        assert resp.routing_reason == "moderate_reasoning"

    async def test_normal_score_threshold_triggers_normal(
        self, settings: Settings, budget: ExpertBudgetService
    ):
        """reasoning_score >= llm_normal_score_threshold triggers normal."""
        settings.llm_normal_score_threshold = 4
        req = _make_request(
            model_tier="fast",
            reasoning_score=5,
        )
        from unittest.mock import AsyncMock
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"moderate"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=budget,
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "normal"

    async def test_cross_source_synthesis_with_score(
        self, settings: Settings, budget: ExpertBudgetService
    ):
        """cross_source_synthesis with high score → expert."""
        req = _make_request(
            model_tier="normal",
            reasoning_score=11,
            hard_signals=["cross_source_synthesis"],
        )
        from unittest.mock import AsyncMock
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"synthesized"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=budget,
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "expert"


class TestRoutingContextValidation:

    async def test_invalid_reasoning_score_rejected(
        self, settings: Settings, budget: ExpertBudgetService
    ):
        req = _make_request(reasoning_score=999)
        with pytest.raises(RoutingValidationError):
            await agent_step(
                client=AsyncMock(),
                settings=settings,
                budget=budget,
                request=req,
                user_id="u-1",
            )

    async def test_unknown_hard_signal_rejected(
        self, settings: Settings, budget: ExpertBudgetService
    ):
        req = _make_request(hard_signals=["made_up_signal"])
        with pytest.raises(RoutingValidationError):
            await agent_step(
                client=AsyncMock(),
                settings=settings,
                budget=budget,
                request=req,
                user_id="u-1",
            )

    async def test_negative_counters_rejected(
        self, settings: Settings, budget: ExpertBudgetService
    ):
        from app.llm.schemas import (
            AgentStepRequest,
            ContextSignals,
            RoutingContext,
            StruggleSignals,
            WeakExecutionSignals,
        )

        ctx = RoutingContext(
            reasoning_score=0,
            hard_reasoning_signals=[],
            weak_signals=WeakExecutionSignals.model_construct(step_count=-1),
            struggle=StruggleSignals(),
            context=ContextSignals(),
        )
        req = AgentStepRequest.model_construct(
            request_id="req-1",
            run_id="run-1",
            model_tier="fast",
            routing_context=ctx,
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )
        with pytest.raises(RoutingValidationError):
            await agent_step(
                client=AsyncMock(),
                settings=settings,
                budget=budget,
                request=req,
                user_id="u-1",
            )
