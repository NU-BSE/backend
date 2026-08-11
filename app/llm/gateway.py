from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from app.core.config import Settings
from app.llm.errors import LLMError
from app.llm.expert_budget import ExpertBudgetService
from app.llm.openrouter import extract_message, extract_usage, openrouter_chat_completion
from app.llm.routing import (
    get_model_id,
    select_effective_tier,
    validate_routing_context,
)
from app.llm.schemas import (
    AgentResult,
    AgentStepRequest,
    AgentStepResponse,
    ModelTier,
    ToolCallResult,
    UsageInfo,
)

logger = logging.getLogger("app.llm.gateway")


def _make_usage_info(raw: dict[str, Any]) -> UsageInfo:
    return UsageInfo(
        prompt_tokens=raw.get("prompt_tokens", 0),
        completion_tokens=raw.get("completion_tokens", 0),
        total_tokens=raw.get("total_tokens", 0),
    )


def to_openrouter_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert frontend AgentToolDefinition list → OpenRouter function-calling format."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        converted.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("inputSchema", tool.get("input_schema", {})),
            },
        })
    return converted


def openrouter_message_to_result(message: dict[str, Any]) -> AgentResult:
    """Convert an OpenRouter choice message into the client's AgentModelResult."""
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        items: list[ToolCallResult] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function") if isinstance(tc.get("function"), dict) else None
            args: dict[str, Any] = {}
            if func and isinstance(func.get("arguments"), str):
                try:
                    args = json.loads(func["arguments"])
                except (json.JSONDecodeError, TypeError):
                    args = {}
            items.append(
                ToolCallResult(
                    id=tc.get("id", ""),
                    tool_name=func.get("name", "") if func else "",
                    args=args,
                )
            )
        if items:
            return AgentResult(kind="tool_calls", tool_calls=items)

    content = message.get("content")
    text = str(content) if isinstance(content, str) else ""
    return AgentResult(kind="final", text=text)


async def agent_step(
    client: httpx.AsyncClient,
    settings: Settings,
    budget: ExpertBudgetService,
    request: AgentStepRequest,
    *,
    user_id: str,
) -> AgentStepResponse:
    ctx = request.routing
    validate_routing_context(ctx)

    effective_tier, reason = select_effective_tier(
        ctx,
        settings,
        expert_budget_ok=True,
        expert_disabled=not settings.llm_allow_expert,
    )

    if effective_tier == "expert":
        reserved = await budget.reserve_expert(user_id=user_id, run_id=request.run_id)
        if not reserved:
            effective_tier, reason = ("normal", "expert_budget_unavailable")

    tried_fallback = False
    current_tier: ModelTier = effective_tier
    current_reason = reason

    while True:
        model_id = get_model_id(current_tier, settings)
        if not model_id:
            raise LLMError(f"No model configured for tier: {current_tier}", code="CONFIG_ERROR")

        try:
            started = time.perf_counter()

            openrouter_tools = (
                to_openrouter_tools(request.tools)
                if request.tools
                else None
            )

            completion = await openrouter_chat_completion(
                client,
                settings,
                model_id=model_id,
                messages=request.messages,
                tools=openrouter_tools,
            )
            latency_ms = round((time.perf_counter() - started) * 1000, 1)

            message = extract_message(completion)
            usage_raw = extract_usage(completion)
            usage_info = _make_usage_info(usage_raw)
            result = openrouter_message_to_result(message)

            logger.info(
                "agent step completed user=%s run=%s tier=%s model=%s latency_ms=%s",
                user_id,
                request.run_id,
                current_tier,
                model_id,
                latency_ms,
            )

            return AgentStepResponse(
                request_id=request.request_id,
                run_id=request.run_id,
                requested_model_tier=ctx.requested_tier,
                effective_model_tier=current_tier,
                routing_reason=current_reason,
                result=result,
                usage=usage_info,
            )

        except Exception as exc:
            if tried_fallback:
                raise

            if isinstance(exc, LLMError) and exc.retryable:
                fallback_tier = _get_fallback_tier(current_tier)
                if fallback_tier != current_tier:
                    tried_fallback = True
                    current_tier = fallback_tier
                    current_reason = "provider_fallback"
                    logger.warning(
                        "provider fallback from %s to %s for run=%s",
                        effective_tier,
                        fallback_tier,
                        request.run_id,
                    )
                    continue

            raise


def _get_fallback_tier(tier: ModelTier) -> ModelTier:
    if tier == "expert":
        return "normal"
    if tier == "normal":
        return "fast"
    return tier
