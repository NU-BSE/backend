"""Onboarding v2 API.

The backend is the source of truth for completed onboarding steps, so the
client can resume anywhere in the flow after a kill or an OAuth roundtrip.
This API only records *intent and progress* — it never grants or claims
permissions. Telegram/Google/Android connection state stays on the device.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.config import Settings
from app.core.errors import ApiError
from app.db.models import User
from app.schemas.onboarding import (
    FirstTaskCompleteRequest,
    FirstTaskStartRequest,
    FirstTaskStartResponse,
    OnboardingFeedbackRequest,
    OnboardingStateResponse,
    UpdateAiModeRequest,
    UpdateIntentsRequest,
)
from app.services import onboarding
from app.services.model_catalog import ModelCatalog

logger = logging.getLogger("app.onboarding")

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


def _catalog(request: Request) -> ModelCatalog:
    return request.app.state.model_catalog


def _settings(request: Request) -> Settings:
    return request.app.state.settings


async def _state(
    request: Request,
    db: AsyncSession,
    user: User,
) -> OnboardingStateResponse:
    record = await onboarding.get_or_create(db, user)
    return await onboarding.build_state(db, record, _settings(request), _catalog(request))


@router.get("/me", response_model=OnboardingStateResponse)
async def get_onboarding(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OnboardingStateResponse:
    return await _state(request, db, user)


@router.patch("/me/intents", response_model=OnboardingStateResponse)
async def update_intents(
    body: UpdateIntentsRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OnboardingStateResponse:
    unknown = [item for item in body.intents if item not in onboarding.VALID_INTENTS]
    if unknown:
        raise ApiError(422, "INVALID_INTENT", f"Unsupported intents: {', '.join(unknown)}")

    record = await onboarding.get_or_create(db, user)
    await onboarding.set_intents(
        db,
        record,
        intents=body.intents,
        custom_intent=body.custom_intent,
    )
    return await onboarding.build_state(db, record, _settings(request), _catalog(request))


@router.patch("/me/ai-mode", response_model=OnboardingStateResponse)
async def update_ai_mode(
    body: UpdateAiModeRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OnboardingStateResponse:
    record = await onboarding.get_or_create(db, user)

    if body.mode == "local":
        effective = await _state(request, db, user)
        if effective.ai_mode_options.local.status == "local_not_supported":
            raise ApiError(
                422,
                "LOCAL_MODEL_NOT_SUPPORTED",
                "Local models are not supported on this deployment/device.",
                extra={"status": "local_not_supported"},
            )

    await onboarding.set_ai_mode(db, record, mode=body.mode)
    return await onboarding.build_state(db, record, _settings(request), _catalog(request))


@router.post("/me/first-task/start", response_model=FirstTaskStartResponse)
async def start_first_task(
    body: FirstTaskStartRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FirstTaskStartResponse:
    record = await onboarding.get_or_create(db, user)
    conversation_id = await onboarding.start_first_task(
        db,
        record,
        suggested_task_id=body.suggested_task_id,
        original_prompt=body.original_prompt,
        conversation_id=body.conversation_id,
    )
    state = await onboarding.build_state(db, record, _settings(request), _catalog(request))
    return FirstTaskStartResponse(conversation_id=conversation_id, state=state)


@router.post("/me/first-task/complete", response_model=OnboardingStateResponse)
async def complete_first_task(
    body: FirstTaskCompleteRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OnboardingStateResponse:
    record = await onboarding.get_or_create(db, user)
    await onboarding.complete_first_task(
        db,
        record,
        conversation_id=body.conversation_id,
        outcome=body.outcome,
        tool_used=body.tool_used,
        tool_names=body.tool_names,
    )
    return await onboarding.build_state(db, record, _settings(request), _catalog(request))


@router.post("/me/feedback", response_model=OnboardingStateResponse)
async def submit_feedback(
    body: OnboardingFeedbackRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OnboardingStateResponse:
    if body.alternative == "other" and not body.alternative_text:
        raise ApiError(422, "ALTERNATIVE_TEXT_REQUIRED", "alternativeText is required for 'other'.")

    record = await onboarding.get_or_create(db, user)
    await onboarding.set_feedback(
        db,
        record,
        result=body.result,
        expectation_text=body.expectation_text,
        alternative=body.alternative,
        alternative_text=body.alternative_text,
    )
    return await onboarding.build_state(db, record, _settings(request), _catalog(request))


@router.post("/me/complete", response_model=OnboardingStateResponse)
async def complete_onboarding(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OnboardingStateResponse:
    record = await onboarding.get_or_create(db, user)
    await onboarding.complete_onboarding(db, record)
    return await onboarding.build_state(db, record, _settings(request), _catalog(request))