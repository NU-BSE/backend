from dataclasses import asdict, dataclass
from time import time_ns
from typing import Any

from app.schemas.attestation import SensitiveAction, TrustTier
from app.services.risk import VelocitySignals

THIRTY_DAYS_MS = 30 * 24 * 60 * 60 * 1000
FIVE_MINUTES_MS = 5 * 60 * 1000


@dataclass(frozen=True)
class AttestationVerdict:
    ok: bool
    platform: str
    signals: dict[str, Any]
    raw_verdict: dict[str, Any]
    hw_backed: bool
    strong_integrity: bool
    first_seen: bool


@dataclass(frozen=True)
class HardwareFingerprint:
    hw_backed: bool
    strong_box: bool
    verified_boot: bool
    os_patch_level_age_days: int
    model_family: str
    software_reported_model: str
    discrepancy: bool
    root_of_trust: str | None = None


@dataclass(frozen=True)
class LatencySignals:
    rtt_ms: int
    nonce_age_ms: int
    clock_skew_ms: int
    latency_anomaly: bool


@dataclass(frozen=True)
class RecentWebAuthn:
    used: bool
    cross_platform: bool
    aaguid_trusted: bool
    verified_at_ms: int | None = None


@dataclass(frozen=True)
class ClassifierInput:
    verdict: AttestationVerdict
    instrumentation_report: Any
    velocity: VelocitySignals
    hardware: HardwareFingerprint
    latency: LatencySignals
    action: SensitiveAction | None = None
    webauthn: RecentWebAuthn | None = None
    strong_integrity_since_ms: int | None = None


@dataclass(frozen=True)
class ClassifierDecision:
    tier: TrustTier
    reasons: list[str]
    trace: dict[str, Any] | None = None


def _financial(action: SensitiveAction | None) -> bool:
    return action is not None and action.type in {"transfer", "addPayee"}


def classify(
    data: ClassifierInput,
    *,
    now_ms: int | None = None,
    debug: bool = False,
) -> ClassifierDecision:
    now_ms = now_ms if now_ms is not None else time_ns() // 1_000_000
    flags = set(data.velocity.flags)
    rules: list[tuple[str, TrustTier, str, bool]] = [
        (
            "verdict_failed",
            TrustTier.BLOCKED,
            "base attestation verdict failed",
            not data.verdict.ok,
        ),
        (
            "hook_without_strong_integrity",
            TrustTier.BLOCKED,
            "hooking framework detected without strong hardware integrity",
            data.instrumentation_report.hook_framework_detected
            and not data.verdict.strong_integrity,
        ),
        (
            "hardware_discrepancy_ip_hopping",
            TrustTier.BLOCKED,
            "hardware/software fingerprint discrepancy with IP hopping",
            data.hardware.discrepancy and "IP_HOPPING" in flags,
        ),
        (
            "financial_unverified_boot",
            TrustTier.BLOCKED,
            "financial action on device without verified root of trust",
            _financial(data.action)
            and data.hardware.root_of_trust is not None
            and data.hardware.root_of_trust != "VERIFIED",
        ),
        (
            "latency_anomaly",
            TrustTier.RESTRICTED,
            "attestation latency anomaly",
            data.latency.latency_anomaly,
        ),
        (
            "burst_or_asn_hopping",
            TrustTier.RESTRICTED,
            "velocity anomaly detected",
            "BURST" in flags or "ASN_HOPPING" in flags,
        ),
        (
            "old_patch_level",
            TrustTier.RESTRICTED,
            "operating system patch level is older than 180 days",
            data.hardware.os_patch_level_age_days > 180,
        ),
        (
            "highest_webauthn",
            TrustTier.HIGHEST,
            "cross-platform trusted WebAuthn ceremony completed recently",
            bool(
                data.webauthn
                and data.webauthn.used
                and data.webauthn.cross_platform
                and data.webauthn.aaguid_trusted
                and data.webauthn.verified_at_ms
                and now_ms - data.webauthn.verified_at_ms <= FIVE_MINUTES_MS
            ),
        ),
        (
            "elevated_strong_integrity",
            TrustTier.ELEVATED,
            "strong integrity has been stable for more than 30 days",
            bool(
                data.verdict.ok
                and data.verdict.strong_integrity
                and data.hardware.hw_backed
                and data.strong_integrity_since_ms
                and now_ms - data.strong_integrity_since_ms >= THIRTY_DAYS_MS
                and not data.velocity.flags
            ),
        ),
        (
            "standard_passing",
            TrustTier.STANDARD,
            "passing attestation with hardware-backed device evidence",
            data.verdict.ok
            and data.hardware.hw_backed
            and not data.latency.latency_anomaly
            and not data.velocity.flags,
        ),
    ]

    matched = [rule for rule in rules if rule[3]]
    block = next((rule for rule in matched if rule[1] == TrustTier.BLOCKED), None)
    restricted = next((rule for rule in matched if rule[1] == TrustTier.RESTRICTED), None)

    if block:
        tier = TrustTier.BLOCKED
    elif restricted:
        tier = TrustTier.RESTRICTED
    else:
        rank = {
            TrustTier.BLOCKED: 0,
            TrustTier.RESTRICTED: 1,
            TrustTier.STANDARD: 2,
            TrustTier.ELEVATED: 3,
            TrustTier.HIGHEST: 4,
        }
        positive = sorted(
            [rule for rule in matched if rule[1] in {TrustTier.STANDARD, TrustTier.ELEVATED, TrustTier.HIGHEST}],
            key=lambda rule: rank[rule[1]],
            reverse=True,
        )
        tier = positive[0][1] if positive else TrustTier.RESTRICTED

    reasons = [rule[2] for rule in matched] or ["no positive trust rule matched"]
    trace = None
    if debug:
        trace = {
            "enabled": True,
            "tier": tier.value,
            "reasons": reasons,
            "evaluatedRules": [
                {
                    "id": rule_id,
                    "matched": is_matched,
                    "contribution": tier_value.value if is_matched else "none",
                }
                for rule_id, tier_value, _reason, is_matched in rules
            ],
            "input": {
                "verdict": asdict(data.verdict),
                "velocity": asdict(data.velocity),
                "hardware": asdict(data.hardware),
                "latency": asdict(data.latency),
            },
        }
    return ClassifierDecision(tier=tier, reasons=reasons, trace=trace)


def minimum_tier_for_action(action: SensitiveAction) -> TrustTier:
    if action.type == "elevatedLogin":
        return TrustTier.HIGHEST
    if action.type == "policyChange":
        return TrustTier.ELEVATED
    if action.type == "transfer" and action.amount >= 10_000:
        return TrustTier.HIGHEST
    return TrustTier.STANDARD


def tier_rank(tier: TrustTier) -> int:
    return {
        TrustTier.BLOCKED: 0,
        TrustTier.RESTRICTED: 1,
        TrustTier.STANDARD: 2,
        TrustTier.ELEVATED: 3,
        TrustTier.HIGHEST: 4,
    }[tier]
