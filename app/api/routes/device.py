"""Device attestation and model licensing.

The flow is two round trips, and the split is deliberate:

1. ``POST /device/nonce`` — the server issues a challenge and remembers it.
2. The app builds ``requestHash`` from that nonce plus its own key, build and
   model version, asks Play for an integrity token bound to that hash, and
   sends both here.
3. ``POST /device/license`` — the server recomputes the hash from *its* copy
   of every field, has Google decode the token, and only then issues a licence
   carrying the model key wrapped to the device's public key.

The nonce exists so a verdict cannot be replayed. Without it an attacker could
present a genuine token — obtained legitimately on their own unrooted device —
against a licence request from a rooted one.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.db.models import User
from app.schemas.common import CamelModel
from app.services.device_license import (
    DEFAULT_LICENSE_TTL_SECONDS,
    NONCE_TTL_SECONDS,
    LicenseError,
    decode_public_key,
    device_key_hash,
    issue_nonce,
    load_model_key,
    request_hash,
    sign_license,
    wrap_model_key,
)
from app.services.play_integrity import (
    IntegrityError,
    PlayIntegrityClient,
    check_verdict,
)

router = APIRouter(prefix="/device", tags=["device"])


class NonceResponse(CamelModel):
    nonce: str
    expires_in: int


class LicenseRequest(CamelModel):
    nonce: str
    integrity_token: str
    device_public_key: str = Field(description="Base64 DER SubjectPublicKeyInfo.")
    app_version: str
    model_version: str


class LicenseResponse(CamelModel):
    license: str
    wrapped_model_key: str
    expires_at: int


"""In-process nonce store.

Deliberately not the database: these live five minutes, are consumed once, and
writing them through a table would add a row per launch for no benefit. The
cost is that a multi-process deployment must either run one worker or move this
to a shared cache — noted here because it is the kind of assumption that
silently breaks on the day the service is scaled out.
"""
_NONCES: dict[str, float] = {}


def _remember(nonce: str) -> None:
    now = time.time()
    # Opportunistic sweep: the store is small and this runs rarely enough that
    # a dedicated task would be more machinery than the problem deserves.
    for key, expiry in list(_NONCES.items()):
        if expiry < now:
            _NONCES.pop(key, None)
    _NONCES[nonce] = now + NONCE_TTL_SECONDS


def _consume(nonce: str) -> bool:
    """Single use: a nonce accepted twice is not a nonce."""
    expiry = _NONCES.pop(nonce, None)
    return expiry is not None and expiry >= time.time()


logger = logging.getLogger("app")


@router.post("/nonce")
async def device_nonce(
    _user: User = Depends(get_current_user),
) -> NonceResponse:
    nonce = issue_nonce()
    _remember(nonce)
    return NonceResponse(nonce=nonce, expires_in=NONCE_TTL_SECONDS)


@router.post("/license")
async def device_license(
    request: Request,
    body: LicenseRequest,
    _user: User = Depends(get_current_user),
) -> LicenseResponse:
    settings = request.app.state.settings

    if not _consume(body.nonce):
        # Covers unknown, expired and already-used in one answer: telling a
        # caller which of the three it was only helps someone probing.
        raise ApiError(400, "BAD_NONCE", "That challenge is unknown or expired.")

    try:
        device_der = decode_public_key(body.device_public_key)
        expected_hash = request_hash(
            nonce=body.nonce,
            device_public_key_der=device_der,
            app_version=body.app_version,
            model_version=body.model_version,
        )
    except LicenseError as exc:
        raise ApiError(400, "BAD_DEVICE_KEY", str(exc)) from exc

    client = PlayIntegrityClient(settings, request.app.state.http_client)
    if not client.configured:
        raise ApiError(503, "INTEGRITY_UNAVAILABLE", "Device licensing is not configured.")

    try:
        verdict = await client.decode(body.integrity_token)
        check_verdict(
            verdict,
            expected_package=settings.play_package_name,
            expected_request_hash=expected_hash,
            require_strong_integrity=settings.require_strong_integrity,
        )
    except IntegrityError as exc:
        # 403 rather than 400: the request was well formed, the device was not
        # accepted.
        raise ApiError(403, "INTEGRITY_FAILED", str(exc)) from exc

    try:
        model_key = load_model_key(settings, body.model_version)
        wrapped = wrap_model_key(model_key, device_der)
        token, expires_at = sign_license(
            settings,
            device_public_key_der=device_der,
            app_version=body.app_version,
            model_version=body.model_version,
            capabilities=["inference"],
            strong_integrity=verdict.meets_strong_integrity,
            ttl_seconds=DEFAULT_LICENSE_TTL_SECONDS,
        )
    except LicenseError as exc:
        raise ApiError(403, "LICENSE_DENIED", str(exc)) from exc

    # Identifier only — never the wrapped key or the licence itself.
    logger.info(
        "device licence issued key=%s model=%s strong=%s",
        device_key_hash(device_der)[:16],
        body.model_version,
        verdict.meets_strong_integrity,
    )

    return LicenseResponse(
        license=token,
        wrapped_model_key=wrapped,
        expires_at=expires_at,
    )
