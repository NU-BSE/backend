from types import SimpleNamespace

from app.schemas.attestation import ElevatedLoginAction, TransferAction, TrustTier
from app.services.classifier import (
    AttestationVerdict,
    ClassifierInput,
    HardwareFingerprint,
    LatencySignals,
    RecentWebAuthn,
    classify,
)
from app.services.risk import VelocitySignals


def base_input(**overrides):
    values = {
        "verdict": AttestationVerdict(
            ok=True,
            platform="android",
            signals={},
            raw_verdict={},
            hw_backed=True,
            strong_integrity=True,
            first_seen=False,
        ),
        "instrumentation_report": SimpleNamespace(hook_framework_detected=False),
        "velocity": VelocitySignals(1, 1, 1, 1, 3.5, []),
        "hardware": HardwareFingerprint(
            hw_backed=True,
            strong_box=False,
            verified_boot=True,
            os_patch_level_age_days=10,
            model_family="Pixel 9 Pro",
            software_reported_model="Pixel 9 Pro",
            discrepancy=False,
            root_of_trust="VERIFIED",
        ),
        "latency": LatencySignals(500, 500, 0, False),
        "action": TransferAction(
            type="transfer",
            amount=100,
            currency="KZT",
            toAccountId="account-1",
        ),
        "strong_integrity_since_ms": None,
        "webauthn": None,
    }
    values.update(overrides)
    return ClassifierInput(**values)


def test_standard_passing() -> None:
    result = classify(base_input(), now_ms=1_000_000)
    assert result.tier == TrustTier.STANDARD


def test_failed_verdict_is_blocked() -> None:
    verdict = AttestationVerdict(False, "android", {}, {}, False, False, False)
    result = classify(base_input(verdict=verdict), now_ms=1_000_000)
    assert result.tier == TrustTier.BLOCKED


def test_unverified_root_blocks_financial_action() -> None:
    hardware = HardwareFingerprint(
        True,
        False,
        False,
        10,
        "Pixel 9 Pro",
        "Pixel 9 Pro",
        False,
        "UNVERIFIED",
    )
    result = classify(base_input(hardware=hardware), now_ms=1_000_000)
    assert result.tier == TrustTier.BLOCKED


def test_old_patch_is_restricted() -> None:
    hardware = HardwareFingerprint(
        True,
        False,
        True,
        181,
        "Pixel 9 Pro",
        "Pixel 9 Pro",
        False,
        "VERIFIED",
    )
    result = classify(base_input(hardware=hardware), now_ms=1_000_000)
    assert result.tier == TrustTier.RESTRICTED


def test_recent_trusted_webauthn_is_highest() -> None:
    now = 10_000_000
    action = ElevatedLoginAction(type="elevatedLogin")
    webauthn = RecentWebAuthn(True, True, True, now - 60_000)
    result = classify(base_input(action=action, webauthn=webauthn), now_ms=now)
    assert result.tier == TrustTier.HIGHEST
