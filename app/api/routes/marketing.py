from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Request

from app.core.config import Settings
from app.core.errors import ApiError
from app.kv.store import TTLStore
from app.schemas.marketing import DownloadInviteRequest, DownloadInviteResponse
from app.services.brevo_marketing import BrevoMarketingService

logger = logging.getLogger("app.marketing")
router = APIRouter(prefix="/marketing", tags=["marketing"])


def _contact_key(email: str) -> str:
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
    return f"marketing:download-invite:email:{digest}"


def _client_ip(request: Request) -> str:
    # Cloudflare sets this header at the edge. Fall back to the socket peer for
    # local development and deployments that do not sit behind Cloudflare.
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client is not None else "unknown"
    )


@router.post("/download-invite", response_model=DownloadInviteResponse)
async def request_download_invite(
    payload: DownloadInviteRequest,
    request: Request,
) -> DownloadInviteResponse:
    settings: Settings = request.app.state.settings
    store: TTLStore = request.app.state.store
    service: BrevoMarketingService = request.app.state.brevo_marketing_service

    email = str(payload.email).strip().lower()
    cooldown_key = _contact_key(email)
    if await store.get(cooldown_key) is not None:
        # Treat a quick retry/double-click as success without firing a second
        # automation email.
        return DownloadInviteResponse(ok=True, already_requested=True)

    ip_key = f"marketing:download-invite:ip:{_client_ip(request)}"
    ip_count = await store.incr(ip_key, 3600)
    if ip_count > settings.marketing_invite_max_per_ip_per_hour:
        retry_after = max(await store.ttl(ip_key), 1)
        raise ApiError(
            429,
            "DOWNLOAD_INVITE_RATE_LIMITED",
            "Too many download-link requests. Please try again later.",
            retryable=True,
            extra={"retryAfterSeconds": retry_after},
        )

    source_path = payload.source_path if payload.source_path.startswith("/") else "/"

    try:
        await service.request_download_invite(
            email=email,
            billing=payload.billing,
            source_path=source_path,
        )
    except RuntimeError as exc:
        logger.exception("Brevo download-invite request failed")
        raise ApiError(
            502,
            "DOWNLOAD_INVITE_UNAVAILABLE",
            "We couldn't request your download link right now. Please try again.",
            retryable=True,
        ) from exc

    await store.set(
        cooldown_key,
        "1",
        settings.marketing_invite_cooldown_seconds,
    )
    return DownloadInviteResponse(ok=True, already_requested=False)
