import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import service_account

from app.core.config import Settings
from app.core.errors import ApiError
from app.services.classifier import AttestationVerdict
from app.services.crypto import sha256_hex

PLAY_INTEGRITY_SCOPE = "https://www.googleapis.com/auth/playintegrity"


@dataclass(frozen=True)
class AndroidVerification:
    verdict: AttestationVerdict
    device_id: str
    model_family: str | None
    os_patch_level: str | None
    decoded: dict[str, Any]


class GooglePlayIntegrityVerifier:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http_client = http_client
        self._credentials = None
        if settings.google_service_account_file:
            self._credentials = service_account.Credentials.from_service_account_file(
                str(settings.google_service_account_file),
                scopes=[PLAY_INTEGRITY_SCOPE],
            )

    @property
    def configured(self) -> bool:
        return bool(
            self._credentials
            and self.settings.play_integrity_project_number
            and self.settings.android_package_name
        )

    async def _access_token(self) -> str:
        if not self._credentials:
            raise ApiError(
                503,
                "INTEGRITY_SERVICE_UNAVAILABLE",
                "Google Play Integrity credentials are not configured",
                retryable=True,
            )
        if not self._credentials.valid or self._credentials.expired:
            await asyncio.to_thread(self._credentials.refresh, GoogleRequest())
        if not self._credentials.token:
            raise ApiError(503, "INTEGRITY_SERVICE_UNAVAILABLE", "Could not obtain Google token")
        return self._credentials.token

    async def decode(self, integrity_token: str) -> dict[str, Any]:
        access_token = await self._access_token()
        url = (
            "https://playintegrity.googleapis.com/v1/"
            f"{self.settings.android_package_name}:decodeIntegrityToken"
        )
        try:
            response = await self.http_client.post(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                json={"integrityToken": integrity_token},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ApiError(
                502,
                "INTEGRITY_SERVICE_UNAVAILABLE",
                "Google Play Integrity verification failed",
                retryable=True,
            ) from exc
        payload = response.json().get("tokenPayloadExternal")
        if not isinstance(payload, dict):
            raise ApiError(502, "INTEGRITY_SERVICE_UNAVAILABLE", "Invalid Google response")
        return payload

    def normalize(self, decoded: dict[str, Any], expected_request_hash: str) -> AndroidVerification:
        request_details = decoded.get("requestDetails") or {}
        app_integrity = decoded.get("appIntegrity") or {}
        device_integrity = decoded.get("deviceIntegrity") or {}
        account_details = decoded.get("accountDetails") or {}
        device_recall = decoded.get("deviceRecall") or {}
        attributes = device_integrity.get("deviceAttributes") or {}

        verdicts = device_integrity.get("deviceRecognitionVerdict") or []
        presented_certs = {
            str(value).replace(":", "").lower()
            for value in app_integrity.get("certificateSha256Digest") or []
        }
        expected_certs = self.settings.android_certificate_allowlist
        if not expected_certs:
            raise ApiError(
                500,
                "SERVER_MISCONFIGURED",
                "Android certificate allowlist is empty",
            )

        request_hash = request_details.get("requestHash") or request_details.get("nonce")
        signals = {
            "requestHashOk": request_hash == expected_request_hash,
            "packageOk": request_details.get("requestPackageName")
            == self.settings.android_package_name,
            "certificateOk": bool(presented_certs & expected_certs),
            "appRecognized": app_integrity.get("appRecognitionVerdict") == "PLAY_RECOGNIZED",
            "licensed": account_details.get("appLicensingVerdict") == "LICENSED",
            "deviceIntegrity": "MEETS_DEVICE_INTEGRITY" in verdicts
            or "MEETS_STRONG_INTEGRITY" in verdicts,
            "strongIntegrity": "MEETS_STRONG_INTEGRITY" in verdicts,
            "sdkVersion": attributes.get("sdkVersion"),
        }
        ok = all(
            signals[key]
            for key in (
                "requestHashOk",
                "packageOk",
                "certificateOk",
                "appRecognized",
                "licensed",
                "deviceIntegrity",
            )
        )

        recall_handle = (device_recall.get("values") or {}).get("deviceHandle")
        if recall_handle:
            device_id = f"pi:{sha256_hex(str(recall_handle))}"
        else:
            # Compatibility fallback only. Replace with a public-key-derived app-instance ID.
            material = ":".join(
                str(value or "")
                for value in (
                    request_details.get("requestPackageName"),
                    attributes.get("manufacturer"),
                    attributes.get("model"),
                    attributes.get("sdkVersion"),
                    *sorted(presented_certs),
                )
            )
            device_id = f"pi-fallback:{sha256_hex(material)}"

        verdict = AttestationVerdict(
            ok=ok,
            platform="android",
            signals=signals,
            raw_verdict={
                "appRecognitionVerdict": app_integrity.get("appRecognitionVerdict"),
                "deviceRecognitionVerdict": verdicts,
                "appLicensingVerdict": account_details.get("appLicensingVerdict"),
                "certificateDigestMatched": signals["certificateOk"],
                "sdkVersion": attributes.get("sdkVersion"),
            },
            hw_backed=bool(signals["deviceIntegrity"]),
            strong_integrity=bool(signals["strongIntegrity"]),
            first_seen=False,
        )
        return AndroidVerification(
            verdict=verdict,
            device_id=device_id,
            model_family=attributes.get("model"),
            os_patch_level=attributes.get("osPatchLevel"),
            decoded=decoded,
        )

@dataclass(slots=True)
class AndroidIntegrityResult:
    ok: bool
    strong_integrity: bool
    device_integrity: bool
    package_name: str
    certificate_ok: bool
    model_family: str | None
    os_patch_level: str | None
    raw: dict


class DevelopmentPlayIntegrityVerifier:
    def __init__(self, settings: Settings) -> None:
        if settings.environment != "development":
            raise RuntimeError(
                "DevelopmentPlayIntegrityVerifier cannot run outside development"
            )

        self._settings = settings

    async def verify(
        self,
        token: str,
        expected_request_hash: str,
    ) -> AndroidIntegrityResult:
        if self._settings.environment != "development":
            raise RuntimeError(
                "Development verifier is disabled outside development"
            )

        return AndroidIntegrityResult(
            ok=True,
            strong_integrity=False,
            device_integrity=True,
            package_name=self._settings.android_package_name,
            certificate_ok=True,
            model_family="development-device",
            os_patch_level=None,
            raw={
                "developmentMock": True,
                "requestHash": expected_request_hash,
            },
        )

from typing import Protocol


class PlayIntegrityVerifier(Protocol):
    async def verify(
        self,
        token: str,
        expected_request_hash: str,
    ) -> AndroidIntegrityResult:
        ...


def create_play_integrity_verifier(
    settings: Settings,
) -> PlayIntegrityVerifier:
    if settings.play_integrity_enabled:
        return GooglePlayIntegrityVerifier(settings)

    if settings.environment == "development":
        return DevelopmentPlayIntegrityVerifier(settings)

    raise RuntimeError(
        "PLAY_INTEGRITY_ENABLED must be true outside development"
    )