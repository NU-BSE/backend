from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Request

from app.api.deps import get_app_settings, get_current_user, get_http_client
from app.core.config import Settings
from app.core.errors import ApiError
from app.db.models import User
from app.kv.store import TTLStore
from app.llm.errors import LLMError, RoutingValidationError
from app.llm.expert_budget import ExpertBudgetService
from app.llm.gateway import agent_step
from app.llm.schemas import AgentStepRequest, AgentStepResponse

logger = logging.getLogger("app.agent")

router = APIRouter(prefix="", tags=["agent"])


def _get_budget_service(request: Request) -> ExpertBudgetService:
    # Use a per-request instance; TTLStore is shared via app.state
    settings: Settings = request.app.state.settings
    store: TTLStore = request.app.state.store
    return ExpertBudgetService(store, settings)


@router.post("/agent/step", response_model=AgentStepResponse)
async def agent_step_endpoint(
    body: AgentStepRequest,
    request: Request,
    user: User = Depends(get_current_user),
    client: httpx.AsyncClient = Depends(get_http_client),
    settings: Settings = Depends(get_app_settings),
    budget: ExpertBudgetService = Depends(_get_budget_service),
) -> AgentStepResponse:
    try:
        response = await agent_step(
            client=client,
            settings=settings,
            budget=budget,
            request=body,
            user_id=user.user_id,
        )
        return response
    except RoutingValidationError as exc:
        raise ApiError(422, exc.code, str(exc)) from exc
    except LLMError as exc:
        status_code = 502 if exc.retryable else 400
        raise ApiError(
            status_code,
            exc.code,
            str(exc),
            retryable=exc.retryable,
        ) from exc
