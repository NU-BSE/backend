from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.llm.schemas import ModelTier, RoutingContext


@dataclass(frozen=True)
class ModelSelection:
    requested_tier: ModelTier
    effective_tier: ModelTier
    model_id: str
    reason: str
    expert_budget_used: bool


def has_emergency_expert_trigger(ctx: RoutingContext) -> bool:
    s = ctx.struggle
    return (
        s.failed_plans >= 2
        or s.repeated_tool_pattern
        or s.invalid_tool_calls >= 3
        or s.replans >= 2
    )


def has_hard_reasoning_signal(ctx: RoutingContext) -> bool:
    return bool(ctx.hard_reasoning_signals)


def _expert_by_reasoning(ctx: RoutingContext, settings: Settings) -> bool:
    return (
        ctx.reasoning_score >= settings.llm_expert_score_threshold
        and has_hard_reasoning_signal(ctx)
    )


def select_effective_tier(
    requested_tier: ModelTier,
    ctx: RoutingContext,
    settings: Settings,
    *,
    expert_budget_ok: bool = True,
    expert_disabled: bool | None = None,
) -> tuple[ModelTier, str]:
    _expert_disabled = (
        expert_disabled if expert_disabled is not None else not settings.llm_allow_expert
    )

    emergency = has_emergency_expert_trigger(ctx)
    expert_by_reasoning = _expert_by_reasoning(ctx, settings)

    expert_justified = emergency or expert_by_reasoning

    if expert_justified and not _expert_disabled and expert_budget_ok:
        reason = "planner_stuck" if emergency else "hard_reasoning"
        return ("expert", reason)

    if expert_justified and (_expert_disabled or not expert_budget_ok):
        reason = "expert_disabled" if _expert_disabled else "expert_budget_unavailable"
        return ("normal", reason)

    normal_triggers = (
        requested_tier in ("normal", "expert")
        or ctx.reasoning_score >= settings.llm_normal_score_threshold
        or ctx.struggle.replans >= 1
    )

    if normal_triggers:
        return ("normal", "moderate_reasoning")

    return ("fast", "default_fast")


def get_model_id(tier: ModelTier, settings: Settings) -> str:
    mapping = {
        "fast": settings.llm_model_fast,
        "normal": settings.llm_model_normal,
        "expert": settings.llm_model_expert,
    }
    return mapping[tier]


_MODEL_ALIASES: dict[ModelTier, str] = {
    "fast": "fast-v1",
    "normal": "normal-v1",
    "expert": "expert-v1",
}


def get_model_alias(tier: ModelTier) -> str:
    return _MODEL_ALIASES[tier]


def validate_routing_context(ctx: RoutingContext) -> None:
    from app.llm.errors import RoutingValidationError

    if not (0 <= ctx.reasoning_score <= 100):
        raise RoutingValidationError(
            f"reasoning_score must be 0-100, got {ctx.reasoning_score}"
        )

    for signal in ctx.hard_reasoning_signals:
        from app.llm.schemas import VALID_HARD_REASONING_SIGNALS

        if signal not in VALID_HARD_REASONING_SIGNALS:
            raise RoutingValidationError(f"unknown hard reasoning signal: {signal}")

    for name in ("step_count", "tool_calls", "connector_count"):
        val = getattr(ctx.weak_signals, name)
        if not (0 <= val <= 1000):
            raise RoutingValidationError(f"{name} must be 0-1000, got {val}")

    for name in ("failed_plans", "replans", "invalid_tool_calls", "repeated_tool_failures"):
        val = getattr(ctx.struggle, name)
        if not (0 <= val <= 1000):
            raise RoutingValidationError(f"{name} must be 0-1000, got {val}")

    if not (0 <= ctx.escalation_count <= 1000):
        raise RoutingValidationError(
            f"escalation_count must be 0-1000, got {ctx.escalation_count}"
        )
