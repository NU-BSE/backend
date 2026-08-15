from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

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


class WeakExecutionSignals(CamelModel):
    step_count: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    connector_count: int = Field(default=0, ge=0)


class StruggleSignals(CamelModel):
    failed_plans: int = Field(default=0, ge=0)
    replans: int = Field(default=0, ge=0)
    repeated_tool_pattern: bool = False
    invalid_tool_calls: int = Field(default=0, ge=0)
    repeated_tool_failures: int = Field(default=0, ge=0)


class ContextSignals(CamelModel):
    large_structured_context: bool = False
    large_unstructured_context: bool = False


class RoutingContext(CamelModel):
    requested_tier: ModelTier = "fast"
    reasoning_score: int = Field(default=0, ge=0)
    hard_reasoning_signals: list[str] = Field(default_factory=list)
    weak_signals: WeakExecutionSignals = Field(default_factory=WeakExecutionSignals)
    struggle: StruggleSignals = Field(default_factory=StruggleSignals)
    context: ContextSignals = Field(default_factory=ContextSignals)
    escalation_count: int = Field(default=0, ge=0)


class UsageInfo(CamelModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ConnectionSummary(CamelModel):
    id: str
    provider: str
    display_name: str
    capabilities: list[str] = Field(default_factory=list)


class AgentStepRequest(CamelModel):
    request_id: str
    run_id: str
    routing: RoutingContext = Field(default_factory=RoutingContext)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    connections: list[ConnectionSummary] = Field(default_factory=list)


class ToolCallResult(CamelModel):
    id: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)


class AgentResult(CamelModel):
    kind: Literal["final", "tool_calls"]
    text: str | None = None
    tool_calls: list[ToolCallResult] | None = None


class AgentStepResponse(CamelModel):
    request_id: str
    run_id: str
    requested_model_tier: ModelTier
    effective_model_tier: ModelTier
    routing_reason: str
    result: AgentResult
    usage: UsageInfo | None = None
