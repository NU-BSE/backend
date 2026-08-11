from __future__ import annotations

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
from app.llm.schemas import AgentStepRequest, AgentStepResponse, ModelTier, UsageInfo

logger = logging.getLogger("app.llm.gateway")


def _make_usage_info(raw: dict[str, Any]) -> UsageInfo:
    return UsageInfo(
        prompt_tokens=raw.get("prompt_tokens", 0),
        completion_tokens=raw.get("completion_tokens", 0),
        total_tokens=raw.get("total_tokens", 0),
    )


async def agent_step(
    client: httpx.AsyncClient,
    settings: Settings,
    budget: ExpertBudgetService,
    request: AgentStepRequest,
    *,
    user_id: str,
) -> AgentStepResponse:
    validate_routing_context(request.routing_context)

    effective_tier, reason = select_effective_tier(
        request.model_tier,
        request.routing_context,
        settings,
        expert_budget_ok=True,
        expert_disabled=not settings.llm_allow_expert,
    )

    if effective_tier == "expert":
        if not await budget.can_use_expert(user_id=user_id, run_id=request.run_id):
            effective_tier, reason = ("normal", "expert_budget_unavailable")
        else:
            await budget.record_expert_use(user_id=user_id, run_id=request.run_id)

    tried_fallback = False
    current_tier: ModelTier = effective_tier
    current_reason = reason

    while True:
        model_id = get_model_id(current_tier, settings)
        if not model_id:
            raise LLMError(f"No model configured for tier: {current_tier}", code="CONFIG_ERROR")

        try:
            started = time.perf_counter()
            completion = await openrouter_chat_completion(
                client,
                settings,
                model_id=model_id,
                messages=request.messages,
                tools=request.tools if request.tools else None,
            )
            latency_ms = round((time.perf_counter() - started) * 1000, 1)

            message = extract_message(completion)
            usage_raw = extract_usage(completion)
            usage_info = _make_usage_info(usage_raw)

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
                requested_model_tier=request.model_tier,
                effective_model_tier=current_tier,
                routing_reason=current_reason,
                message=message,
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
    return tier
