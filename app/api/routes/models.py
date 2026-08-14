"""Model weight delivery.

Onboarding chooses a memory profile and then pays; the weights for that profile
are downloaded from here. Both halves are enforced — the profile must exist and
the subscription must be live — because these are the largest objects the
service serves and the only ones with a per-byte cost.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.errors import ApiError
from app.db.models import User
from app.schemas.models import (
    ModelBundleResponse,
    ModelCatalogResponse,
    ModelFileResponse,
)
from app.services import entitlements
from app.services.model_catalog import ModelBundle, ModelCatalog

logger = logging.getLogger("app.models")

router = APIRouter(prefix="/models", tags=["models"])


def _download_url(profile: str, name: str) -> str:
    return f"/models/{profile}/files/{name}"


def _bundle_response(bundle: ModelBundle) -> ModelBundleResponse:
    return ModelBundleResponse(
        profile=bundle.profile,
        model=bundle.model,
        total_bytes=bundle.total_bytes,
        files=[
            ModelFileResponse(
                name=item.name,
                role=item.role,
                bytes=item.bytes,
                sha256=item.sha256,
                url=_download_url(bundle.profile, item.name),
            )
            for item in bundle.files
        ],
    )


async def _may_download(
    request: Request, db: AsyncSession, user: User
) -> tuple[bool, str | None]:
    """Whether this user's subscription currently covers a download.

    Resolved through the same entitlement path the agent gate uses, so a
    refund processed by RTDN closes both at once. Entitlements carry the paid
    period's expiry, so a lapsed subscription stops qualifying without anything
    having to sweep it.
    """
    settings = request.app.state.settings
    if not settings.model_download_requires_entitlement:
        return True, None

    effective = await entitlements.sync_for_user(db, user.user_id)
    if effective.cloud_agent_allowed:
        return True, effective.plan_code
    return False, effective.plan_code


@router.get("/catalog", response_model=ModelCatalogResponse)
async def catalog(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModelCatalogResponse:
    """What is available per profile, with sizes and hashes.

    Deliberately readable without a subscription: onboarding shows the download
    size on the model-selection screen, which comes before the paywall. Telling
    someone a download is 431 MB costs nothing; sending the 431 MB does.
    """
    models: ModelCatalog = request.app.state.model_catalog
    allowed, _ = await _may_download(request, db, user)

    return ModelCatalogResponse(
        bundles=[
            _bundle_response(bundle)
            for bundle in (models.bundle(profile) for profile in models.profiles)
            if bundle is not None
        ],
        download_allowed=allowed,
    )


@router.get("/{profile}/files/{name}")
async def download(
    profile: str,
    name: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    models: ModelCatalog = request.app.state.model_catalog

    allowed, plan_code = await _may_download(request, db, user)
    if not allowed:
        raise ApiError(
            402,
            "SUBSCRIPTION_REQUIRED",
            "Model weights are available once your subscription is active.",
            extra={"planCode": plan_code},
        )

    entry = models.file(profile, name)
    if entry is None:
        # One message for "no such profile" and "no such file". Enumerating
        # what exists here would only help someone probing the filesystem.
        raise ApiError(404, "MODEL_FILE_NOT_FOUND", "No such model file.")

    logger.info(
        "serving %s/%s (%d bytes) to %s", profile, name, entry.bytes, user.user_id
    )
    return FileResponse(
        entry.path,
        media_type="application/octet-stream",
        filename=entry.name,
        headers={
            # Lets a client verify the download without re-reading the
            # catalogue, and survives a redirect through a CDN.
            "X-Model-Sha256": entry.sha256,
            # Weights are immutable once exported; a changed model is a
            # different filename, so caching them forever is safe and saves
            # re-downloading a gigabyte after an app reinstall.
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )
