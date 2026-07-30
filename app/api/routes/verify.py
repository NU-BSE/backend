from dataclasses import asdict, replace
from datetime import UTC
from time import time_ns

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_asn_lookup,
    get_db,
    get_jit_signer,
    get_nonce_store,
    get_play_integrity_verifier,
    get_risk_provider,
)
from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.core.security import CurrentUser, get_current_user
from app.db.repositories import AttestationResultRepository, DeviceRepository
from app.schemas.attestation import (
    AndroidAttestationSuccess,
    AttestationFailure,
    TrustTier,
    VerifyRequest,
    VerifySuccess,
)
from app.services.classifier import (
    ClassifierInput,
    LatencySignals,
    classify,
    minimum_tier_for_action,
    tier_rank,
)
from app.services.crypto import (
    canonicalize_model,
    compute_attestation_request_hash,
    compute_payload_hash,
)
from app.services.hardware import (
    conservative_hardware_fingerprint,
    derive_leaf_public_key_device_id,
)
from app.services.jit import LocalFileEd25519Signer
from app.services.nonce import RedisNonceStore
from app.services.play_integrity import GooglePlayIntegrityVerifier
from app.services.risk import RedisRiskProvider, StaticAsnLookup

router = APIRouter(prefix="/attest", tags=["attestation"])


def _software_model(body: VerifyRequest) -> str | None:
    report = body.instrumentation_report
    if report.device_model:
        return report.device_model
    android_signals = report.signals.get("androidSignals")
    if isinstance(android_signals, dict):
        value = android_signals.get("value")
        if isinstance(value, dict) and isinstance(value.get("model"), str):
            return value["model"]
    return None


@router.post("/verify", response_model=VerifySuccess)
async def verify_attestation(
    body: VerifyRequest,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
    nonce_store: RedisNonceStore = Depends(get_nonce_store),
    verifier: GooglePlayIntegrityVerifier = Depends(get_play_integrity_verifier),
    risk_provider: RedisRiskProvider = Depends(get_risk_provider),
    asn_lookup: StaticAsnLookup = Depends(get_asn_lookup),
    signer: LocalFileEd25519Signer = Depends(get_jit_signer),
) -> VerifySuccess:
    canonical_json = canonicalize_model(body.action)
    if len(canonical_json.encode("utf-8")) > 4 * 1024:
        raise ApiError(400, "PAYLOAD_BINDING_INVALID", "Canonical action exceeds 4 KB")
    if canonical_json != body.canonical_json:
        raise ApiError(
            400,
            "PAYLOAD_BINDING_INVALID",
            "canonicalJson does not match the canonical form of action",
        )
    payload_hash = compute_payload_hash(canonical_json)

    nonce = await nonce_store.consume(body.nonce, user.user_id)
    now_ms = time_ns() // 1_000_000
    rtt_ms = now_ms - nonce.server_issued_at
    latency = LatencySignals(
        rtt_ms=rtt_ms,
        nonce_age_ms=rtt_ms,
        clock_skew_ms=nonce.client_ts - nonce.server_issued_at,
        latency_anomaly=rtt_ms > settings.max_attested_latency_ms or rtt_ms < 0,
    )

    if isinstance(body.attestation, AttestationFailure):
        raise ApiError(
            400,
            body.attestation.code,
            body.attestation.details or "Client attestation failed",
            retryable=body.attestation.retryable,
        )
    if body.attestation.platform != body.instrumentation_report.platform:
        raise ApiError(400, "UNKNOWN", "Attestation platform does not match instrumentation")
    if not isinstance(body.attestation, AndroidAttestationSuccess):
        raise ApiError(501, "DEVICE_NOT_SUPPORTED", "iOS App Attest is not enabled yet")

    expected_request_hash = compute_attestation_request_hash(body.nonce, payload_hash)
    decoded = await verifier.decode(body.attestation.token)
    android = verifier.normalize(decoded, expected_request_hash)

    chain = (
        body.attestation.hardware_key_attestation.certificate_chain_base64
        if body.attestation.hardware_key_attestation
        else None
    )
    key_device_id = derive_leaf_public_key_device_id(chain or [])
    device_id = key_device_id or android.device_id

    device_repo = DeviceRepository(db)
    device, first_seen = await device_repo.bind_or_update(
        device_id=device_id,
        user_id=user.user_id,
        platform="android",
        model_family=android.model_family,
        strong_integrity=android.verdict.strong_integrity,
    )
    verdict = replace(android.verdict, first_seen=first_seen)

    hardware = conservative_hardware_fingerprint(
        settings=settings,
        chain_base64=chain,
        play_device_integrity=verdict.hw_backed,
        model_family=android.model_family,
        software_reported_model=_software_model(body),
        os_patch_level=android.os_patch_level,
    )

    request_ip = request.client.host if request.client else ""
    asn = await asn_lookup.lookup(request_ip)
    velocity = await risk_provider.evaluate(
        device_id=device_id,
        now_ms=now_ms,
        nonce=body.nonce,
        ip=request_ip,
        asn=asn,
        payload_hash=payload_hash,
        first_seen=first_seen,
    )
    strong_since_ms = (
        int(device.strong_integrity_since.astimezone(UTC).timestamp() * 1000)
        if device.strong_integrity_since
        else None
    )
    decision = classify(
        ClassifierInput(
            verdict=verdict,
            instrumentation_report=body.instrumentation_report,
            velocity=velocity,
            hardware=hardware,
            latency=latency,
            action=body.action,
            strong_integrity_since_ms=strong_since_ms,
        ),
        debug=settings.app_env == "development" and settings.app_debug,
    )

    if decision.tier == TrustTier.BLOCKED:
        await db.commit()
        raise ApiError(
            403,
            "BLOCKED",
            "; ".join(decision.reasons),
            extra={"classifierTrace": decision.trace} if decision.trace else None,
        )

    required_tier = minimum_tier_for_action(body.action)
    financial = body.action.type in {"transfer", "addPayee"}
    if tier_rank(decision.tier) < tier_rank(required_tier) or (
        decision.tier == TrustTier.RESTRICTED and financial
    ):
        await db.commit()
        raise ApiError(
            403,
            "REQUIRES_ELEVATION",
            f"Action requires {required_tier.value} trust tier",
            retryable=True,
            extra={
                "requiredTier": required_tier.value,
                **({"classifierTrace": decision.trace} if decision.trace else {}),
            },
        )

    token, expires_at = signer.issue(
        user_id=user.user_id,
        tier=decision.tier,
        action_hash=payload_hash,
        device_id=device_id,
    )
    await AttestationResultRepository(db).add(
        user_id=user.user_id,
        device_id=device_id,
        platform="android",
        trust_tier=decision.tier.value,
        action_hash=payload_hash,
        verdict={
            **asdict(verdict),
            "hardware": asdict(hardware),
        },
        classifier_reasons=decision.reasons,
        risk_flags=velocity.flags,
    )
    await db.commit()
    return VerifySuccess(
        accessToken=token,
        tier=decision.tier,
        expiresAt=expires_at,
        classifierTrace=decision.trace,
    )
