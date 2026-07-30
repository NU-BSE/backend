import base64
from datetime import UTC, date, datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, ed448, padding, rsa

from app.core.config import Settings
from app.services.classifier import HardwareFingerprint
from app.services.crypto import sha256_hex


def _verify_signature(child: x509.Certificate, issuer: x509.Certificate) -> None:
    key = issuer.public_key()
    signature = child.signature
    data = child.tbs_certificate_bytes
    algorithm = child.signature_hash_algorithm
    if isinstance(key, rsa.RSAPublicKey):
        key.verify(signature, data, padding.PKCS1v15(), algorithm)
    elif isinstance(key, ec.EllipticCurvePublicKey):
        key.verify(signature, data, ec.ECDSA(algorithm))
    elif isinstance(key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
        key.verify(signature, data)
    else:
        raise ValueError("Unsupported certificate public key")


def _parse_patch_age(value: str | None) -> int:
    if not value:
        return 9_999
    try:
        patch = date.fromisoformat(value)
    except ValueError:
        return 9_999
    return max(0, (datetime.now(UTC).date() - patch).days)


def derive_leaf_public_key_device_id(chain_base64: list[str]) -> str | None:
    if not chain_base64:
        return None
    try:
        leaf = x509.load_der_x509_certificate(base64.b64decode(chain_base64[0], validate=True))
        spki = leaf.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except (ValueError, TypeError):
        return None
    return f"key:{sha256_hex(spki)}"


def conservative_hardware_fingerprint(
    *,
    settings: Settings,
    chain_base64: list[str] | None,
    play_device_integrity: bool,
    model_family: str | None,
    software_reported_model: str | None,
    os_patch_level: str | None,
) -> HardwareFingerprint:
    chain_trusted = False
    if chain_base64:
        try:
            certs = [
                x509.load_der_x509_certificate(base64.b64decode(item, validate=True))
                for item in chain_base64
            ]
            for child, issuer in zip(certs, certs[1:], strict=False):
                if child.issuer != issuer.subject:
                    raise ValueError("Certificate issuer mismatch")
                _verify_signature(child, issuer)
            root_digest = certs[-1].fingerprint(hashes.SHA256()).hex().lower()
            chain_trusted = root_digest in settings.android_root_allowlist
        except (ValueError, TypeError):
            chain_trusted = False

    software_model = software_reported_model or "unknown"
    trusted_model = model_family or software_model
    discrepancy = (
        trusted_model != "unknown"
        and software_model != "unknown"
        and trusted_model.lower() not in software_model.lower()
    )

    # Play Integrity may support STANDARD access. Financial actions remain blocked because
    # root_of_trust is not VERIFIED until KeyDescription ASN.1 parsing is implemented.
    return HardwareFingerprint(
        hw_backed=play_device_integrity,
        strong_box=False,
        verified_boot=False,
        os_patch_level_age_days=_parse_patch_age(os_patch_level),
        model_family=trusted_model,
        software_reported_model=software_model,
        discrepancy=discrepancy,
        root_of_trust="UNVERIFIED" if chain_trusted or play_device_integrity else "FAILED",
    )
