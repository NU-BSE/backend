"""Device-bound model licences.

A licence says: *this* installation, on *this* device, running *this* build,
may decrypt *this* model version, until *this* moment. It is useless anywhere
else, because the model key inside it is wrapped to a public key whose private
half never leaves the device's hardware keystore.

Three decisions worth stating, because the obvious alternatives are all
weaker:

* **Signed asymmetrically, not with the app's HMAC secret.** The app must
  verify the licence, so whatever verifies it ships inside the app. A shared
  HMAC secret would therefore be extractable from any build, and anyone
  holding it could mint their own licences. The app carries only a public key.

* **The model key is wrapped per device, not shared.** A single global model
  key that every install decrypts is one extraction away from being universal.
  Wrapping to the device key means a key recovered from one rooted phone
  decrypts nothing on any other.

* **Short-lived, and re-issued.** An indefinite licence is a permanent grant
  to a device that may since have been rooted. Expiry is what makes revocation
  possible at all: `model_versions` that leak simply stop being licensed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import jwt

from app.core.config import Settings


class LicenseError(Exception):
    """The licence could not be issued."""


# A licence outlives a session but not a day. Long enough that a user is not
# re-attested constantly, short enough that a device which becomes compromised
# loses access without waiting for a key rotation.
DEFAULT_LICENSE_TTL_SECONDS = 12 * 60 * 60

# The nonce is only alive long enough to complete one attestation round trip.
NONCE_TTL_SECONDS = 5 * 60


def issue_nonce() -> str:
    """A single-use, unpredictable challenge for one licence request."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def request_hash(
    *,
    nonce: str,
    device_public_key_der: bytes,
    app_version: str,
    model_version: str,
) -> str:
    """The value the client must bind into its Play Integrity request.

    Everything that identifies the request is folded in, so a verdict cannot be
    moved to a different device, build, model or challenge. Google echoes this
    back in the verdict and the server recomputes it from its own copy of each
    field — the client never gets to assert what it hashed.
    """
    digest = hashlib.sha256()
    digest.update(nonce.encode())
    digest.update(b"\x00")
    digest.update(hashlib.sha256(device_public_key_der).digest())
    digest.update(b"\x00")
    digest.update(app_version.encode())
    digest.update(b"\x00")
    digest.update(model_version.encode())
    return base64.urlsafe_b64encode(digest.digest()).decode().rstrip("=")


def device_key_hash(device_public_key_der: bytes) -> str:
    """Stable identifier for a device key, safe to log and store."""
    return hashlib.sha256(device_public_key_der).hexdigest()


@dataclass(frozen=True)
class IssuedLicense:
    token: str
    """Signed licence, verified in the app with the bundled public key."""
    wrapped_model_key: str
    """The model key, encrypted to the device's public key. Base64."""
    expires_at: int


def wrap_model_key(model_key: bytes, device_public_key_der: bytes) -> str:
    """Encrypt the model key to the device's hardware-backed public key.

    RSA-OAEP because an Android Keystore RSA key can decrypt without the
    private half ever being exported — the unwrap happens inside the TEE or
    StrongBox, and the plaintext model key exists only in the app's memory
    afterwards.

    That last part is the limit of this design, and it is worth being precise
    about: the key is protected in transit and at rest, not against an attacker
    who already controls the running process.
    """
    try:
        public_key = serialization.load_der_public_key(device_public_key_der)
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same answer
        raise LicenseError("The device public key could not be read.") from exc

    if not isinstance(public_key, rsa.RSAPublicKey):
        raise LicenseError("The device key must be RSA for key wrapping.")

    if public_key.key_size < 2048:
        raise LicenseError("The device key is too small.")

    wrapped = public_key.encrypt(
        model_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(wrapped).decode()


def sign_license(
    settings: Settings,
    *,
    device_public_key_der: bytes,
    app_version: str,
    model_version: str,
    capabilities: list[str],
    strong_integrity: bool,
    ttl_seconds: int = DEFAULT_LICENSE_TTL_SECONDS,
) -> tuple[str, int]:
    """Sign a licence bound to one device key, build and model version."""
    private_pem = settings.license_signing_key
    if not private_pem:
        raise LicenseError("No licence signing key is configured.")

    now = int(time.time())
    expires_at = now + ttl_seconds
    claims = {
        "sub": device_key_hash(device_public_key_der),
        "appVersion": app_version,
        "modelVersion": model_version,
        "capabilities": capabilities,
        # Recorded so the app can refuse to run a model on a device that only
        # met basic integrity, if a future model demands more.
        "strongIntegrity": strong_integrity,
        "iat": now,
        "exp": expires_at,
        "iss": settings.jwt_issuer,
        "aud": "creepy-im-model",
    }

    try:
        token = jwt.encode(claims, private_pem, algorithm="RS256")
    except Exception as exc:  # noqa: BLE001
        raise LicenseError(f"The licence could not be signed: {exc}") from exc

    return token, expires_at


def decode_public_key(encoded: str) -> bytes:
    """Decode a base64 DER SubjectPublicKeyInfo from the client."""
    try:
        der = base64.b64decode(encoded, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise LicenseError("The device public key is not valid base64.") from exc
    if not der:
        raise LicenseError("The device public key is empty.")
    return der


def load_model_key(settings: Settings, model_version: str) -> bytes:
    """The symmetric key the model shards were encrypted with.

    Keyed by model version so a leaked version can be retired without
    invalidating the others: the server simply stops issuing licences for it,
    and every future build ships shards under a new key.
    """
    raw = settings.model_keys_json
    if not raw:
        raise LicenseError("No model keys are configured.")
    try:
        keys = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LicenseError("MODEL_KEYS_JSON is not valid JSON.") from exc

    encoded = keys.get(model_version)
    if not encoded:
        # Revocation path: an unknown or retired version is simply not licensed.
        raise LicenseError(f"Model version {model_version} is not licensed.")
    try:
        return base64.b64decode(encoded, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise LicenseError(f"The key for {model_version} is not valid base64.") from exc
