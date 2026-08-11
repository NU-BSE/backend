from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings
from app.kv.store import MemoryTTLStore
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


def _budget(settings: Settings) -> ExpertBudgetService:
    return ExpertBudgetService(MemoryTTLStore(), settings)


def _make_request(
    requested_tier: str = "fast",
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
        routing=RoutingContext(
            requested_tier=requested_tier,  # type: ignore[arg-type]
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


class TestAgentStepRouting:

    async def test_fast_stays_fast(self, settings: Settings):
        """Backend never upgrades from fast."""
        req = _make_request(requested_tier="fast", reasoning_score=2, tool_calls=8)
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=_budget(settings),
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "fast"

    async def test_fast_stays_fast_even_with_emergency(
        self, settings: Settings
    ):
        """Backend never upgrades from fast even with planner stuck."""
        req = _make_request(requested_tier="fast", failed_plans=3)
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=_budget(settings),
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "fast"

    async def test_normal_stays_normal(self, settings: Settings):
        req = _make_request(requested_tier="normal", reasoning_score=5)
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=_budget(settings),
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "normal"

    async def test_expert_with_evidence_allowed(
        self, settings: Settings
    ):
        """Expert requested + hard reasoning signals → allowed."""
        req = _make_request(
            requested_tier="expert",
            reasoning_score=12,
            hard_signals=["constraint_solving"],
        )
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"solved"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=_budget(settings),
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "expert"
        assert resp.routing_reason == "hard_reasoning"

    async def test_expert_without_evidence_downgraded(
        self, settings: Settings
    ):
        """Expert requested with no hard signals → downgraded."""
        req = _make_request(requested_tier="expert", reasoning_score=2)
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"nope"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=_budget(settings),
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "normal"

    async def test_expert_disabled_downgrades(
        self, settings: Settings
    ):
        settings.llm_allow_expert = False
        req = _make_request(
            requested_tier="expert",
            reasoning_score=15,
            hard_signals=["constraint_solving"],
        )
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"normal"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=_budget(settings),
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "normal"
        assert resp.routing_reason == "expert_disabled"
        settings.llm_allow_expert = True

    async def test_expert_budget_denied_downgrades(
        self, settings: Settings
    ):
        settings.llm_expert_daily_user_limit = 0
        req = _make_request(
            requested_tier="expert",
            reasoning_score=15,
            hard_signals=["cross_source_synthesis"],
        )
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"fallback"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=_budget(settings),
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "normal"
        assert resp.routing_reason == "expert_budget_unavailable"
        settings.llm_expert_daily_user_limit = 20

    async def test_planner_stuck_expert_allowed(
        self, settings: Settings
    ):
        """Expert requested + planner stuck → allowed."""
        req = _make_request(requested_tier="expert", failed_plans=2)
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"rescued"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=_budget(settings),
            request=req,
            user_id="u-1",
        )
        assert resp.effective_model_tier == "expert"
        assert resp.routing_reason == "planner_stuck"

    async def test_result_has_tool_calls(self, settings: Settings):
        """Verify tool_calls are converted to AgentResult."""
        req = _make_request(requested_tier="normal")
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=(
                b'{"choices":[{"message":{"role":"assistant","content":null,'
                b'"tool_calls":[{"id":"call_1","function":{"name":"search",'
                b'"arguments":"{\\"query\\":\\"Daniyar\\"}"}}]}}]}'
            )
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=_budget(settings),
            request=req,
            user_id="u-1",
        )
        assert resp.result.kind == "tool_calls"
        assert resp.result.tool_calls is not None
        assert resp.result.tool_calls[0].tool_name == "search"
        assert resp.result.tool_calls[0].args == {"query": "Daniyar"}

    async def test_result_final(self, settings: Settings):
        """Verify text response is converted to AgentResult."""
        req = _make_request(requested_tier="fast")
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"hello there"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=_budget(settings),
            request=req,
            user_id="u-1",
        )
        assert resp.result.kind == "final"
        assert resp.result.text == "hello there"

    async def test_tool_converter_agent_to_openrouter(
        self, settings: Settings
    ):
        """Verify frontend tools are converted to OpenRouter format."""
        req = _make_request(
            requested_tier="normal",
            tools=[{
                "name": "search_chats",
                "description": "Search Telegram chats",
                "inputSchema": {"type": "object", "properties": {}},
            }],
        )
        mock_client = AsyncMock()
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.aread = AsyncMock(
            return_value=b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
        )

        resp = await agent_step(
            client=mock_client,  # type: ignore[arg-type]
            settings=settings,
            budget=_budget(settings),
            request=req,
            user_id="u-1",
        )

        call_kwargs = mock_client.post.call_args
        tools_sent = call_kwargs[1]["json"]["tools"]
        assert len(tools_sent) == 1
        assert tools_sent[0]["type"] == "function"
        assert tools_sent[0]["function"]["name"] == "search_chats"

        assert resp.effective_model_tier == "normal"


class TestRoutingContextValidation:

    async def test_invalid_reasoning_score_rejected(
        self, settings: Settings
    ):
        req = _make_request(reasoning_score=999)
        with pytest.raises(RoutingValidationError):
            await agent_step(
                client=AsyncMock(),
                settings=settings,
                budget=_budget(settings),
                request=req,
                user_id="u-1",
            )

    async def test_unknown_hard_signal_rejected(
        self, settings: Settings
    ):
        req = _make_request(hard_signals=["made_up_signal"])
        with pytest.raises(RoutingValidationError):
            await agent_step(
                client=AsyncMock(),
                settings=settings,
                budget=_budget(settings),
                request=req,
                user_id="u-1",
            )

    async def test_negative_counters_rejected(
        self, settings: Settings
    ):
        from app.llm.schemas import (
            AgentStepRequest,
            ContextSignals,
            RoutingContext,
            StruggleSignals,
            WeakExecutionSignals,
        )

        ctx = RoutingContext(
            requested_tier="fast",
            reasoning_score=0,
            hard_reasoning_signals=[],
            weak_signals=WeakExecutionSignals.model_construct(step_count=-1),
            struggle=StruggleSignals(),
            context=ContextSignals(),
        )
        req = AgentStepRequest.model_construct(
            request_id="req-1",
            run_id="run-1",
            routing=ctx,
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )
        with pytest.raises(RoutingValidationError):
            await agent_step(
                client=AsyncMock(),
                settings=settings,
                budget=_budget(settings),
                request=req,
                user_id="u-1",
            )
