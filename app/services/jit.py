from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.config import Settings
from app.schemas.attestation import TrustTier
from app.services.crypto import b64url


class LocalFileEd25519Signer:
    def __init__(self, private_key: Ed25519PrivateKey, settings: Settings) -> None:
        self.private_key = private_key
        self.settings = settings

    @classmethod
    def from_file(cls, path: Path, settings: Settings) -> "LocalFileEd25519Signer":
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError("JIT private key must be Ed25519")
        return cls(key, settings)

    def public_jwk(self) -> dict[str, str]:
        raw = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        return {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": b64url(raw),
            "kid": self.settings.jit_kid,
            "alg": "EdDSA",
            "use": "sig",
        }

    def _ttl(self, tier: TrustTier) -> int:
        return {
            TrustTier.RESTRICTED: self.settings.jit_ttl_restricted_seconds,
            TrustTier.STANDARD: self.settings.jit_ttl_standard_seconds,
            TrustTier.ELEVATED: self.settings.jit_ttl_elevated_seconds,
            TrustTier.HIGHEST: self.settings.jit_ttl_highest_seconds,
        }[tier]

    def issue(
        self,
        *,
        user_id: str,
        tier: TrustTier,
        action_hash: str,
        device_id: str,
    ) -> tuple[str, int]:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self._ttl(tier))
        claims: dict[str, Any] = {
            "sub": user_id,
            "aud": self.settings.jit_api_audience,
            "tier": tier.value,
            "actionHash": action_hash,
            "deviceId": device_id,
            "nonceConsumed": True,
            "iat": now,
            "exp": expires,
        }
        token = jwt.encode(
            claims,
            self.private_key,
            algorithm="EdDSA",
            headers={"kid": self.settings.jit_kid, "typ": "JWT"},
        )
        return token, int(expires.timestamp() * 1000)
