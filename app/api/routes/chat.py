import logging
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.config import Settings
from app.core.errors import ApiError
from app.db.models import User
from app.db.repositories import new_id
from app.schemas.chat import ChatHttpRequest
from app.services import entitlements
from app.services.llm import agent_run_events, encode_event
from app.services.usage import UsageMeter

logger = logging.getLogger("app.agent")

router = APIRouter(tags=["agent"])


@router.post("/chat/http")
async def chat_http(
    request: Request,
    body: ChatHttpRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    settings: Settings = request.app.state.settings
    usage_meter: UsageMeter = request.app.state.usage_meter
    client: httpx.AsyncClient = request.app.state.http_client

    effective = await entitlements.sync_for_user(db, user.user_id, settings)

    if not effective.agent_access:
        raise ApiError(
            403,
            "AGENT_ACCESS_REQUIRED",
            "Your plan does not include the agent.",
            extra={"planCode": effective.plan_code},
        )
    if not effective.cloud_agent_allowed:
        raise ApiError(
            403,
            "CLOUD_AGENT_NOT_ALLOWED",
            "The cloud agent requires Pro. Upgrade to continue.",
            extra={"planCode": effective.plan_code},
        )

    check = await usage_meter.check_and_increment(
        user.user_id, effective.max_agent_messages_per_day
    )
    if not check.allowed:
        raise ApiError(
            402,
            "USAGE_LIMIT_REACHED",
            "Daily agent message limit reached. It resets at midnight UTC.",
            retryable=True,
            extra={
                "retryAfterSeconds": check.retry_after_seconds,
                "limit": check.limit,
            },
        )

    thread_id = body.thread_id or new_id("thread")
    run_id = body.run_id or new_id("run")
    message_id = new_id("msg")

    logger.info(
        "agent run user=%s thread=%s plan=%s",
        user.user_id,
        thread_id,
        effective.plan_code,
    )

    events = agent_run_events(
        client,
        settings,
        thread_id=thread_id,
        run_id=run_id,
        message_id=message_id,
        messages=body.messages,
    )

    async def ndjson() -> AsyncIterator[bytes]:
        async for event in events:
            yield encode_event(event)

    return StreamingResponse(
        ndjson(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Request-ID": getattr(request.state, "request_id", ""),
        },
    )
