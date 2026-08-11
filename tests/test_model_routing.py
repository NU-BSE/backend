import pytest

from app.core.config import Settings
from app.llm.errors import RoutingValidationError
from app.llm.routing import (
    ModelSelection,
    get_model_alias,
    get_model_id,
    has_emergency_expert_trigger,
    has_hard_reasoning_signal,
    select_effective_tier,
    validate_routing_context,
)
from app.llm.schemas import (
    ContextSignals,
    RoutingContext,
    StruggleSignals,
    WeakExecutionSignals,
)


def _ctx(
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
) -> RoutingContext:
    return RoutingContext(
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
    )


def _settings(**kwargs) -> Settings:
    defaults: dict[str, object] = {
        "jwt_secret": "test-secret-" + "x" * 40,
        "brevo_api_key": "test",
        "email_from": "test@test.com",
        "llm_allow_expert": True,
        "llm_normal_score_threshold": 4,
        "llm_expert_score_threshold": 10,
        "llm_model_fast": "anthropic/claude-3-haiku",
        "llm_model_normal": "anthropic/claude-3-sonnet",
        "llm_model_expert": "openai/gpt-4o",
    }
    return Settings(_env_file=None, **defaults, **kwargs)  # type: ignore[arg-type]


class TestHasEmergencyExpertTrigger:

    def test_failed_plans_two_triggers(self):
        ctx = _ctx(failed_plans=2)
        assert has_emergency_expert_trigger(ctx) is True

    def test_failed_plans_one_does_not_trigger(self):
        ctx = _ctx(failed_plans=1)
        assert has_emergency_expert_trigger(ctx) is False

    def test_repeated_tool_pattern_triggers(self):
        ctx = _ctx(repeated_tool_pattern=True)
        assert has_emergency_expert_trigger(ctx) is True

    def test_invalid_tool_calls_three_triggers(self):
        ctx = _ctx(invalid_tool_calls=3)
        assert has_emergency_expert_trigger(ctx) is True

    def test_invalid_tool_calls_two_does_not_trigger(self):
        ctx = _ctx(invalid_tool_calls=2)
        assert has_emergency_expert_trigger(ctx) is False

    def test_replans_two_triggers(self):
        ctx = _ctx(replans=2)
        assert has_emergency_expert_trigger(ctx) is True

    def test_replans_one_does_not_trigger(self):
        ctx = _ctx(replans=1)
        assert has_emergency_expert_trigger(ctx) is False

    def test_no_triggers(self):
        ctx = _ctx()
        assert has_emergency_expert_trigger(ctx) is False


class TestHasHardReasoningSignal:

    def test_with_signals(self):
        ctx = _ctx(hard_signals=["constraint_solving"])
        assert has_hard_reasoning_signal(ctx) is True

    def test_without_signals(self):
        ctx = _ctx(hard_signals=[])
        assert has_hard_reasoning_signal(ctx) is False


class TestSelectEffectiveTier:

    def _select(self, ctx, settings=None, *, expert_budget_ok=True, expert_disabled=None):
        s = settings or _settings()
        return select_effective_tier(
            ctx,
            s,
            expert_budget_ok=expert_budget_ok,
            expert_disabled=expert_disabled,
        )

    def test_fast_request_stays_fast(self):
        """Fast requested + no hard signals → fast (backend never upgrades)."""
        ctx = _ctx(requested_tier="fast", reasoning_score=2, tool_calls=8)
        tier, reason = self._select(ctx)
        assert tier == "fast"
        assert reason == "default_fast"

    def test_fast_request_stays_fast_even_with_high_score(self):
        """Fast requested but high score → still fast (backend never upgrades)."""
        ctx = _ctx(
            requested_tier="fast",
            reasoning_score=12,
            hard_signals=["constraint_solving"],
        )
        tier, reason = self._select(ctx)
        assert tier == "fast"
        assert reason == "default_fast"

    def test_fast_request_stays_fast_even_with_planner_stuck(self):
        """Even planner stuck with fast request → stays fast (frontend owns routing)."""
        ctx = _ctx(requested_tier="fast", failed_plans=3)
        tier, _reason = self._select(ctx)
        assert tier == "fast"

    def test_normal_request_stays_normal(self):
        ctx = _ctx(requested_tier="normal", reasoning_score=3)
        tier, reason = self._select(ctx)
        assert tier == "normal"
        assert reason == "moderate_reasoning"

    def test_expert_request_with_hard_reasoning_gets_expert(self):
        """Expert requested + hard signals + high score → expert allowed."""
        ctx = _ctx(
            requested_tier="expert",
            reasoning_score=12,
            hard_signals=["constraint_solving", "ranking_or_optimization"],
        )
        tier, reason = self._select(ctx)
        assert tier == "expert"
        assert reason == "hard_reasoning"

    def test_expert_request_without_evidence_downgraded(self):
        """Expert requested but no evidence → downgrade to normal."""
        ctx = _ctx(requested_tier="expert", reasoning_score=2)
        tier, reason = self._select(ctx)
        assert tier == "normal"
        assert reason == "expert_not_justified"

    def test_expert_request_with_planner_stuck_gets_expert(self):
        """Expert requested + emergency trigger → expert allowed."""
        ctx = _ctx(requested_tier="expert", failed_plans=2)
        tier, reason = self._select(ctx)
        assert tier == "expert"
        assert reason == "planner_stuck"

    def test_expert_disabled_downgrades(self):
        ctx = _ctx(
            requested_tier="expert",
            reasoning_score=15,
            hard_signals=["constraint_solving"],
        )
        tier, reason = self._select(ctx, expert_disabled=True)
        assert tier == "normal"
        assert reason == "expert_disabled"

    def test_expert_budget_denied_downgrades(self):
        ctx = _ctx(
            requested_tier="expert",
            reasoning_score=15,
            hard_signals=["constraint_solving"],
        )
        tier, reason = self._select(ctx, expert_budget_ok=False)
        assert tier == "normal"
        assert reason == "expert_budget_unavailable"

    def test_normal_with_high_score_stays_normal(self):
        """Normal request with expert-level score stays normal (never upgrades)."""
        ctx = _ctx(
            requested_tier="normal",
            reasoning_score=15,
            hard_signals=["constraint_solving"],
        )
        tier, _reason = self._select(ctx)
        assert tier == "normal"

    def test_malicious_expert_score_100_no_signal_downgraded(self):
        """Score 100 but no hard reasoning signal → downgraded."""
        ctx = _ctx(requested_tier="expert", reasoning_score=100)
        tier, reason = self._select(ctx)
        assert tier == "normal"
        assert reason == "expert_not_justified"


class TestValidateRoutingContext:

    def test_valid_context(self):
        ctx = _ctx(requested_tier="normal", reasoning_score=50, hard_signals=["constraint_solving"])
        validate_routing_context(ctx)

    def test_score_too_high(self):
        ctx = _ctx(reasoning_score=101)
        with pytest.raises(RoutingValidationError):
            validate_routing_context(ctx)

    def test_score_negative(self):
        ctx = RoutingContext.model_construct(reasoning_score=-1)
        with pytest.raises(RoutingValidationError):
            validate_routing_context(ctx)

    def test_unknown_signal(self):
        ctx = _ctx(hard_signals=["made_up"])
        with pytest.raises(RoutingValidationError):
            validate_routing_context(ctx)

    def test_valid_signal_accepted(self):
        ctx = _ctx(hard_signals=["cross_source_synthesis", "conflicting_evidence"])
        validate_routing_context(ctx)

    def test_tool_calls_out_of_range(self):
        ctx = _ctx(tool_calls=1001)
        with pytest.raises(RoutingValidationError):
            validate_routing_context(ctx)

    def test_unknown_tier_rejected(self):
        ctx = RoutingContext.model_construct(requested_tier="invalid_tier")  # type: ignore[arg-type]
        with pytest.raises(RoutingValidationError):
            validate_routing_context(ctx)


class TestModelIdAndAlias:

    def test_get_model_id(self):
        settings = Settings(
            _env_file=None,  # type: ignore[arg-type]
            jwt_secret="test-secret-" + "x" * 40,
            brevo_api_key="test",
            email_from="test@test.com",
            llm_model_fast="fast-model",
            llm_model_normal="normal-model",
            llm_model_expert="expert-model",
        )
        assert get_model_id("fast", settings) == "fast-model"
        assert get_model_id("normal", settings) == "normal-model"
        assert get_model_id("expert", settings) == "expert-model"

    def test_get_model_alias(self):
        assert get_model_alias("fast") == "fast-v1"
        assert get_model_alias("normal") == "normal-v1"
        assert get_model_alias("expert") == "expert-v1"


class TestModelSelection:

    def test_model_selection_dataclass(self):
        sel = ModelSelection(
            requested_tier="fast",
            effective_tier="expert",
            model_id="openai/gpt-4o",
            reason="hard_reasoning",
            expert_budget_used=True,
        )
        assert sel.requested_tier == "fast"
        assert sel.effective_tier == "expert"
        assert sel.model_id == "openai/gpt-4o"
        assert sel.reason == "hard_reasoning"
        assert sel.expert_budget_used is True
