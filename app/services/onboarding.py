"""Onboarding v2 — persistent per-user state and step transitions.

The backend is the source of truth for *completed* steps, so a client can
resume after a kill, an OAuth roundtrip, a Telegram handshake, a local-model
download or a network failure. Transient screens (connections, ai-mode) are
recorded once past; the client may repeat them freely until they are.

This module deliberately does not model permissions: intents are interests,
connections live on the device, and nothing here is treated as a granted
scope.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db import repositories as repo
from app.db.models import User, UserOnboarding
from app.db.repositories import new_id
from app.schemas.onboarding import (
    AiMode,
    AiModeCloudOptions,
    AiModeLocalOptions,
    AiModeOptions,
    FeedbackInfo,
    FirstTaskInfo,
    LocalAiStatus,
    OnboardingStateResponse,
    OnboardingStatus,
)
from app.services import entitlements
from app.services.model_catalog import ModelCatalog

logger = logging.getLogger("app.onboarding")

ONBOARDING_VERSION = 2

# The set of intents a user may select in onboarding. Intent == interest,
# never a granted permission: selecting "messages" does not mean Telegram is
# connected, and the agent gate never consults this column.
VALID_INTENTS = frozenset(
    {"android_settings", "messages", "email", "calendar", "drive"}
)

# Ordered from least to most progressed. Transitions only ever move forward.
STATUS_ORDER = (
    "not_started",
    "auth_completed",
    "intent_completed",
    "connections_completed",
    "ai_mode_completed",
    "first_task_started",
    "first_task_completed",
    "feedback_completed",
    "subscription_completed",
    "completed",
)
_STATUS_RANK = {status: rank for rank, status in enumerate(STATUS_ORDER)}

# Statuses that mean "onboarding finished". A v1 user migrated to v2 lands
# here, and auth responses report these as onboarding completed.
COMPLETED_STATUSES = frozenset({"completed", "subscription_completed"})


def _rank(status: str) -> int:
    return _STATUS_RANK.get(status, 0)


def _advance(current: str, target: str) -> str:
    """Move through the flow, never backwards: idempotent step completion."""
    if _rank(target) > _rank(current):
        return target
    return current


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_or_create(session: AsyncSession, user: User) -> UserOnboarding:
    """Load the record, creating it if needed.

    A legacy v1 user (already has a name) is migrated straight to
    "completed": they are never sent back through the full v2 onboarding.
    """
    existing = await repo.get_onboarding(session, user.user_id)
    if existing is not None:
        return existing

    status = "completed" if user.name else "not_started"
    onboarding = await repo.create_onboarding(
        session, user_id=user.user_id, version=ONBOARDING_VERSION, status=status
    )
    if status in COMPLETED_STATUSES:
        logger.info("migrated legacy user %s to onboarding v2 as completed", user.user_id)
    return onboarding


async def complete_after_auth(
    session: AsyncSession, user: User, *, was_new: bool
) -> UserOnboarding:
    """Run after login/registration.

    A brand-new account is at ``auth_completed``. An existing account with no
    record (pre-v2) is migrated to ``completed`` so they are not re-onboarded;
    an existing account that never finished v1 onboarding starts where v1 left
    them off.
    """
    existing = await repo.get_onboarding(session, user.user_id)
    if existing is not None:
        return existing

    if was_new:
        status = "auth_completed"
    elif user.name:
        status = "completed"
    else:
        status = "not_started"
    onboarding = await repo.create_onboarding(
        session, user_id=user.user_id, version=ONBOARDING_VERSION, status=status
    )
    if status in COMPLETED_STATUSES:
        logger.info("migrated legacy user %s to onboarding v2 as completed", user.user_id)
    return onboarding


async def set_intents(
    session: AsyncSession,
    onboarding: UserOnboarding,
    *,
    intents: list[str],
    custom_intent: str | None,
) -> None:
    cleaned = [item for item in intents if item in VALID_INTENTS]
    await repo.update_onboarding(
        session,
        onboarding,
        status=_advance(onboarding.status, "intent_completed"),
        intents=cleaned,
        custom_intent=custom_intent,
    )
    emit(
        "onboarding_intent_completed",
        intents=cleaned,
        has_custom_intent=custom_intent is not None,
    )


async def set_ai_mode(
    session: AsyncSession,
    onboarding: UserOnboarding,
    *,
    mode: str,
) -> None:
    await repo.update_onboarding(
        session,
        onboarding,
        status=_advance(onboarding.status, "ai_mode_completed"),
        ai_mode=mode,
    )
    emit("onboarding_ai_mode_selected", mode=mode)


async def start_first_task(
    session: AsyncSession,
    onboarding: UserOnboarding,
    *,
    suggested_task_id: str | None,
    original_prompt: str | None,
    conversation_id: str | None,
) -> str:
    conv_id = conversation_id or new_id("conv")
    first_task = {
        "conversation_id": conv_id,
        "started_at": _now_iso(),
        "suggested_task_id": suggested_task_id,
        "original_prompt": original_prompt,
    }
    await repo.update_onboarding(
        session,
        onboarding,
        status=_advance(onboarding.status, "first_task_started"),
        first_task=first_task,
    )
    emit(
        "onboarding_first_task_started",
        suggested_task_id=suggested_task_id,
        intents=onboarding.intents,
    )
    return conv_id


async def complete_first_task(
    session: AsyncSession,
    onboarding: UserOnboarding,
    *,
    conversation_id: str | None,
    outcome: str,
    tool_used: bool,
    tool_names: list[str],
) -> None:
    current = dict(onboarding.first_task or {})
    current.update(
        {
            "completed_at": _now_iso(),
            "outcome": outcome,
            "tool_used": tool_used,
            "tool_names": tool_names,
        }
    )
    if conversation_id:
        current["conversation_id"] = conversation_id
    await repo.update_onboarding(
        session,
        onboarding,
        status=_advance(onboarding.status, "first_task_completed"),
        first_task=current,
    )
    emit(
        "onboarding_first_task_agent_completed",
        tool_used=tool_used,
        tool_names=tool_names,
    )


async def set_feedback(
    session: AsyncSession,
    onboarding: UserOnboarding,
    *,
    result: str,
    expectation_text: str | None,
    alternative: str | None,
    alternative_text: str | None,
) -> None:
    await repo.update_onboarding(
        session,
        onboarding,
        status=_advance(onboarding.status, "feedback_completed"),
        feedback={
            "result": result,
            "expectation_text": expectation_text,
            "alternative": alternative,
            "alternative_text": alternative_text,
        },
    )
    emit(
        "onboarding_first_task_feedback",
        result=result,
        alternative=alternative,
    )


async def complete_onboarding(session: AsyncSession, onboarding: UserOnboarding) -> None:
    await repo.update_onboarding(
        session,
        onboarding,
        status=_advance(onboarding.status, "completed"),
    )
    emit("onboarding_completed", status=onboarding.status)


def is_completed(onboarding: UserOnboarding) -> bool:
    return onboarding.status in COMPLETED_STATUSES


def _local_status(
    settings: Settings,
    catalog: ModelCatalog,
    effective: entitlements.EffectiveEntitlements,
) -> str:
    """Report the local-AI availability the backend actually knows about.

    ``local_ready`` (model downloaded and loaded) is device state the backend
    cannot observe; ``local_available`` / ``local_download_required`` /
    ``local_not_supported`` are the backend-side facts it can report. The
    client combines these with its own device assessment.
    """
    if not catalog.profiles:
        return "local_not_supported"
    if not settings.model_download_requires_entitlement:
        return "local_available"
    if effective.cloud_agent_allowed:
        return "local_available"
    return "local_download_required"


def _paywall_due(
    onboarding: UserOnboarding,
    effective: entitlements.EffectiveEntitlements,
) -> bool:
    return (
        effective.subscription_required
        and onboarding.status == "feedback_completed"
    )


async def build_state(
    session: AsyncSession,
    onboarding: UserOnboarding,
    settings: Settings,
    catalog: ModelCatalog,
) -> OnboardingStateResponse:
    effective = await entitlements.sync_for_user(session, onboarding.user_id, settings)
    first_task = onboarding.first_task or {}
    feedback = onboarding.feedback or {}
    return OnboardingStateResponse(
        version=onboarding.version,
        status=cast(OnboardingStatus, onboarding.status),
        intents=onboarding.intents or [],
        custom_intent=onboarding.custom_intent,
        ai_mode=cast(AiMode | None, onboarding.ai_mode),
        first_task=FirstTaskInfo(
            conversation_id=first_task.get("conversation_id"),
            started_at=first_task.get("started_at"),
            completed_at=first_task.get("completed_at"),
            suggested_task_id=first_task.get("suggested_task_id"),
            original_prompt=first_task.get("original_prompt"),
            tool_used=first_task.get("tool_used"),
            tool_names=first_task.get("tool_names") or [],
            outcome=first_task.get("outcome"),
        ),
        feedback=FeedbackInfo(
            result=feedback.get("result"),
            expectation_text=feedback.get("expectation_text"),
            alternative=feedback.get("alternative"),
            alternative_text=feedback.get("alternative_text"),
        ),
        ai_mode_options=AiModeOptions(
            local=AiModeLocalOptions(
                status=cast(LocalAiStatus, _local_status(settings, catalog, effective)),
                profiles=list(catalog.profiles),
                download_allowed=bool(effective.cloud_agent_allowed)
                or not settings.model_download_requires_entitlement,
            ),
            cloud=AiModeCloudOptions(
                requires_subscription=not effective.cloud_agent_allowed
            ),
        ),
        subscription_required=effective.subscription_required,
        paywall_due=_paywall_due(onboarding, effective),
        created_at=onboarding.created_at,
        updated_at=onboarding.updated_at,
    )


def emit(event: str, **props: object) -> None:
    """Safe, structured onboarding event.

    There is no third-party analytics pipeline in this backend, so events are
    logged with metadata only. Free text — user prompts, custom intents, and
    Telegram/Gmail contents — is never sent here.
    """
    logger.info("onboarding_event event=%s props=%s", event, props)