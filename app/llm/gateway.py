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
    ConnectionSummary,
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


def to_openrouter_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []

    for message in messages:
        role = message.get("role")

        if role == "user":
            converted.append({
                "role": "user",
                "content": message.get("content", ""),
            })
            continue

        if role == "assistant":
            tool_calls = message.get("toolCalls")

            if isinstance(tool_calls, list) and tool_calls:
                converted.append({
                    "role": "assistant",
                    "content": message.get("content") or None,
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["toolName"],
                                "arguments": json.dumps(
                                    call.get("args", {}),
                                    ensure_ascii=False,
                                ),
                            },
                        }
                        for call in tool_calls
                    ],
                })
            else:
                converted.append({
                    "role": "assistant",
                    "content": message.get("content", ""),
                })

            continue

        if role == "tool":
            converted.append({
                "role": "tool",
                "tool_call_id": message["toolCallId"],
                "content": json.dumps(
                    message.get("result", {}),
                    ensure_ascii=False,
                ),
            })

    return converted


def build_agent_system_message(
    connections: list[ConnectionSummary],
) -> dict[str, str]:
    if connections:
        lines = [
            (
                f"- id: {connection.id}; "
                f"provider: {connection.provider}; "
                f"name: {connection.display_name}; "
                f"capabilities: {', '.join(connection.capabilities)}"
            )
            for connection in connections
        ]
        connection_text = "\n".join(lines)
    else:
        connection_text = "- none"

    content = f"""
You are the action planner of a mobile AI assistant.

Rules:
- Use only tools provided in the request.
- Never invent tool names.
- Never invent connection IDs.
- Use only connection IDs listed below.
- Tool execution happens locally on the user's device.
- User approval for side effects is handled locally.
- If a required service is not connected, explain that instead of inventing access.

Connected accounts:
{connection_text}
""".strip()

    return {
        "role": "system",
        "content": content,
    }


def openrouter_message_to_result(message: dict[str, Any]) -> AgentResult:
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

            openrouter_messages = [
                build_agent_system_message(request.connections),
                *to_openrouter_messages(request.messages),
            ]

            completion = await openrouter_chat_completion(
                client,
                settings,
                model_id=model_id,
                messages=openrouter_messages,
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
