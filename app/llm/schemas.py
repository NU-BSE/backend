from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import CamelModel

ModelTier = Literal["fast", "normal", "expert"]

HardReasoningSignal = Literal[
    "cross_source_synthesis",
    "conflicting_evidence",
    "constraint_solving",
    "temporal_reconciliation",
    "ranking_or_optimization",
    "dependent_multi_stage_reasoning",
]

VALID_HARD_REASONING_SIGNALS: frozenset[str] = frozenset(
    HardReasoningSignal.__args__  # type: ignore[attr-defined]
)


class WeakExecutionSignals(BaseModel):
    step_count: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    connector_count: int = Field(default=0, ge=0)


class StruggleSignals(BaseModel):
    failed_plans: int = Field(default=0, ge=0)
    replans: int = Field(default=0, ge=0)
    repeated_tool_pattern: bool = False
    invalid_tool_calls: int = Field(default=0, ge=0)
    repeated_tool_failures: int = Field(default=0, ge=0)


class ContextSignals(BaseModel):
    large_structured_context: bool = False
    large_unstructured_context: bool = False


class RoutingContext(BaseModel):
    reasoning_score: int = Field(default=0, ge=0)
    hard_reasoning_signals: list[str] = Field(default_factory=list)
    weak_signals: WeakExecutionSignals = Field(default_factory=WeakExecutionSignals)
    struggle: StruggleSignals = Field(default_factory=StruggleSignals)
    context: ContextSignals = Field(default_factory=ContextSignals)
    escalation_count: int = Field(default=0, ge=0)


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class AgentStepRequest(CamelModel):
    request_id: str
    run_id: str
    model_tier: ModelTier = "fast"
    routing_context: RoutingContext = Field(default_factory=RoutingContext)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)


class AgentStepResponse(CamelModel):
    request_id: str
    run_id: str
    requested_model_tier: ModelTier
    effective_model_tier: ModelTier
    routing_reason: str
    message: dict[str, Any]
    usage: UsageInfo | None = None
