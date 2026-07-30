from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date
from typing import Any

from asn1crypto import core
from cryptography import x509
from cryptography.x509.oid import ObjectIdentifier

ANDROID_KEY_DESCRIPTION_OID = ObjectIdentifier("1.3.6.1.4.1.11129.2.1.17")

# KeyMint enum values used by the current Android client.
KM_ALGORITHM_EC = 3
KM_PURPOSE_SIGN = 2
KM_PURPOSE_VERIFY = 3
KM_DIGEST_SHA256 = 4
KM_EC_CURVE_P256 = 1


class KeyAttestationError(ValueError):
    """Raised when Android Key Attestation is malformed or fails policy checks."""


class SecurityLevel(core.Enumerated):
    _map = {
        0: "software",
        1: "trusted_environment",
        2: "strong_box",
    }


class VerifiedBootState(core.Enumerated):
    _map = {
        0: "verified",
        1: "self_signed",
        2: "unverified",
        3: "failed",
    }


class IntegerSet(core.SetOf):
    _child_spec = core.Integer


class RootOfTrust(core.Sequence):
    _fields = [
        ("verified_boot_key", core.OctetString),
        ("device_locked", core.Boolean),
        ("verified_boot_state", VerifiedBootState),
        # Present in newer attestation schema versions, absent in older ones.
        ("verified_boot_hash", core.OctetString, {"optional": True}),
    ]


class AuthorizationList(core.Sequence):
    # Superset of fields from Keymaster 2 through KeyMint 5. Unknown future fields
    # intentionally fail parsing so production does not silently ignore new semantics.
    _fields = [
        ("purpose", IntegerSet, {"explicit": 1, "optional": True}),
        ("algorithm", core.Integer, {"explicit": 2, "optional": True}),
        ("key_size", core.Integer, {"explicit": 3, "optional": True}),
        ("block_mode", IntegerSet, {"explicit": 4, "optional": True}),
        ("digest", IntegerSet, {"explicit": 5, "optional": True}),
        ("padding", IntegerSet, {"explicit": 6, "optional": True}),
        ("caller_nonce", core.Null, {"explicit": 7, "optional": True}),
        ("min_mac_length", core.Integer, {"explicit": 8, "optional": True}),
        ("ec_curve", core.Integer, {"explicit": 10, "optional": True}),
        ("ml_dsa_variant", core.Integer, {"explicit": 11, "optional": True}),
        ("rsa_public_exponent", core.Integer, {"explicit": 200, "optional": True}),
        ("mgf_digest", IntegerSet, {"explicit": 203, "optional": True}),
        ("rollback_resistance", core.Null, {"explicit": 303, "optional": True}),
        ("early_boot_only", core.Null, {"explicit": 305, "optional": True}),
        ("active_date_time", core.Integer, {"explicit": 400, "optional": True}),
        (
            "origination_expire_date_time",
            core.Integer,
            {"explicit": 401, "optional": True},
        ),
        ("usage_expire_date_time", core.Integer, {"explicit": 402, "optional": True}),
        ("usage_count_limit", core.Integer, {"explicit": 405, "optional": True}),
        ("user_secure_id", core.Integer, {"explicit": 502, "optional": True}),
        ("no_auth_required", core.Null, {"explicit": 503, "optional": True}),
        ("user_auth_type", core.Integer, {"explicit": 504, "optional": True}),
        ("auth_timeout", core.Integer, {"explicit": 505, "optional": True}),
        ("allow_while_on_body", core.Null, {"explicit": 506, "optional": True}),
        (
            "trusted_user_presence_required",
            core.Null,
            {"explicit": 507, "optional": True},
        ),
        (
            "trusted_confirmation_required",
            core.Null,
            {"explicit": 508, "optional": True},
        ),
        (
            "unlocked_device_required",
            core.Null,
            {"explicit": 509, "optional": True},
        ),
        # Legacy Keymaster field.
        ("all_applications", core.Null, {"explicit": 600, "optional": True}),
        ("creation_date_time", core.Integer, {"explicit": 701, "optional": True}),
        ("origin", core.Integer, {"explicit": 702, "optional": True}),
        # Legacy spelling/tag retained for old certificate support.
        ("rollback_resistant", core.Null, {"explicit": 703, "optional": True}),
        ("root_of_trust", RootOfTrust, {"explicit": 704, "optional": True}),
        ("os_version", core.Integer, {"explicit": 705, "optional": True}),
        ("os_patch_level", core.Integer, {"explicit": 706, "optional": True}),
        (
            "attestation_application_id",
            core.OctetString,
            {"explicit": 709, "optional": True},
        ),
        ("attestation_id_brand", core.OctetString, {"explicit": 710, "optional": True}),
        ("attestation_id_device", core.OctetString, {"explicit": 711, "optional": True}),
        ("attestation_id_product", core.OctetString, {"explicit": 712, "optional": True}),
        ("attestation_id_serial", core.OctetString, {"explicit": 713, "optional": True}),
        ("attestation_id_imei", core.OctetString, {"explicit": 714, "optional": True}),
        ("attestation_id_meid", core.OctetString, {"explicit": 715, "optional": True}),
        (
            "attestation_id_manufacturer",
            core.OctetString,
            {"explicit": 716, "optional": True},
        ),
        ("attestation_id_model", core.OctetString, {"explicit": 717, "optional": True}),
        ("vendor_patch_level", core.Integer, {"explicit": 718, "optional": True}),
        ("boot_patch_level", core.Integer, {"explicit": 719, "optional": True}),
        ("device_unique_attestation", core.Null, {"explicit": 720, "optional": True}),
        (
            "attestation_id_second_imei",
            core.OctetString,
            {"explicit": 723, "optional": True},
        ),
        ("module_hash", core.OctetString, {"explicit": 724, "optional": True}),
    ]


class KeyDescription(core.Sequence):
    _fields = [
        ("attestation_version", core.Integer),
        ("attestation_security_level", SecurityLevel),
        ("keymint_version", core.Integer),
        ("keymint_security_level", SecurityLevel),
        ("attestation_challenge", core.OctetString),
        ("unique_id", core.OctetString),
        ("software_enforced", AuthorizationList),
        ("hardware_enforced", AuthorizationList),
    ]


@dataclass(frozen=True)
class ParsedRootOfTrust:
    verified_boot_key: bytes
    device_locked: bool
    verified_boot_state: str
    verified_boot_hash: bytes | None


@dataclass(frozen=True)
class ParsedKeyDescription:
    attestation_version: int
    attestation_security_level: str
    keymint_version: int
    keymint_security_level: str
    attestation_challenge: bytes
    software_enforced: dict[str, Any]
    hardware_enforced: dict[str, Any]
    root_of_trust: ParsedRootOfTrust | None

    @property
    def hardware_backed(self) -> bool:
        return self.attestation_security_level in {
            "trusted_environment",
            "strong_box",
        } and self.keymint_security_level in {
            "trusted_environment",
            "strong_box",
        }

    @property
    def strong_box(self) -> bool:
        return (
            self.attestation_security_level == "strong_box"
            and self.keymint_security_level == "strong_box"
        )


def decode_base64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:
        raise KeyAttestationError("Expected challenge is not valid base64url") from exc


def _native_mapping(value: core.Asn1Value) -> dict[str, Any]:
    native = value.native
    if not isinstance(native, dict):
        raise KeyAttestationError("AuthorizationList is not a mapping")
    return dict(native)


def parse_key_description(extension_der: bytes) -> ParsedKeyDescription:
    try:
        parsed = KeyDescription.load(extension_der, strict=True)
        native = parsed.native
    except (ValueError, TypeError) as exc:
        raise KeyAttestationError("Malformed Android KeyDescription ASN.1") from exc

    if not isinstance(native, dict):
        raise KeyAttestationError("KeyDescription did not decode to a sequence")

    software = _native_mapping(parsed["software_enforced"])
    hardware = _native_mapping(parsed["hardware_enforced"])

    root_native = hardware.get("root_of_trust")
    root: ParsedRootOfTrust | None = None
    if isinstance(root_native, dict):
        root = ParsedRootOfTrust(
            verified_boot_key=bytes(root_native.get("verified_boot_key") or b""),
            device_locked=bool(root_native.get("device_locked")),
            verified_boot_state=str(root_native.get("verified_boot_state") or "unknown"),
            verified_boot_hash=(
                bytes(root_native["verified_boot_hash"])
                if root_native.get("verified_boot_hash") is not None
                else None
            ),
        )

    return ParsedKeyDescription(
        attestation_version=int(native["attestation_version"]),
        attestation_security_level=str(native["attestation_security_level"]),
        keymint_version=int(native["keymint_version"]),
        keymint_security_level=str(native["keymint_security_level"]),
        attestation_challenge=bytes(native["attestation_challenge"]),
        software_enforced=software,
        hardware_enforced=hardware,
        root_of_trust=root,
    )


def find_trusted_key_description_certificate(
    certs_leaf_to_root: list[x509.Certificate],
) -> tuple[x509.Certificate, bytes]:
    """Find the KeyDescription occurrence nearest the trusted root.

    Android's chain is supplied leaf first. Scanning backwards follows Google's
    guidance not to assume the extension is on the leaf and not to trust later,
    attacker-added occurrences closer to the leaf.
    """

    for cert in reversed(certs_leaf_to_root[:-1]):
        try:
            extension = cert.extensions.get_extension_for_oid(ANDROID_KEY_DESCRIPTION_OID)
        except x509.ExtensionNotFound:
            continue

        value = extension.value
        if not isinstance(value, x509.UnrecognizedExtension):
            raise KeyAttestationError("Unexpected parsed type for Android KeyDescription")
        return cert, value.value

    raise KeyAttestationError("Android KeyDescription extension is missing")


def verify_key_description_policy(
    description: ParsedKeyDescription,
    *,
    expected_challenge_base64url: str,
) -> None:
    expected_challenge = decode_base64url(expected_challenge_base64url)
    if description.attestation_challenge != expected_challenge:
        raise KeyAttestationError("attestationChallenge mismatch")

    if not description.hardware_backed:
        raise KeyAttestationError("Attested key is software-backed")

    root = description.root_of_trust
    if root is None:
        raise KeyAttestationError("hardwareEnforced.rootOfTrust is missing")
    if not root.device_locked:
        raise KeyAttestationError("Android bootloader is not locked")
    if root.verified_boot_state != "verified":
        raise KeyAttestationError(
            f"Verified Boot state is {root.verified_boot_state}, expected verified"
        )

    hw = description.hardware_enforced
    purposes = set(hw.get("purpose") or [])
    digests = set(hw.get("digest") or [])

    if hw.get("algorithm") != KM_ALGORITHM_EC:
        raise KeyAttestationError("Attested key algorithm is not EC")
    if hw.get("key_size") != 256:
        raise KeyAttestationError("Attested EC key size is not 256 bits")
    if hw.get("ec_curve") != KM_EC_CURVE_P256:
        raise KeyAttestationError("Attested EC curve is not P-256")
    if not {KM_PURPOSE_SIGN, KM_PURPOSE_VERIFY}.issubset(purposes):
        raise KeyAttestationError("Attested key lacks SIGN/VERIFY purposes")
    if KM_DIGEST_SHA256 not in digests:
        raise KeyAttestationError("Attested key does not authorize SHA-256")


def parse_android_patch_level(value: int | None) -> date | None:
    if value is None:
        return None
    digits = str(value)
    try:
        if len(digits) == 6:  # YYYYMM
            return date(int(digits[:4]), int(digits[4:6]), 1)
        if len(digits) == 8:  # YYYYMMDD
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None
    return None
