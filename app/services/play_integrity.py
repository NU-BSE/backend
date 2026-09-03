"""Play Integrity verdicts, used to decide whether a device may hold a model licence.

The token is decoded by Google rather than parsed here: the decode endpoint is
what proves the token was minted by Play for *our* package, and a token we
parsed ourselves would carry no such guarantee.

Two properties matter more than the verdict fields themselves:

* The token must answer the request we asked about. Play echoes back the
  `requestHash` we supplied, and checking it is what stops an attacker
  replaying a genuine verdict — captured from their own device, or from an
  earlier legitimate request — against a different licence request.
* The verdict must be fresh. Google explicitly advises against caching these
  for licensing decisions, so a verdict older than a couple of minutes is
  rejected rather than reused.

Authentication reuses the same service-account JWT assertion as Play Billing;
only the scope differs.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from app.core.config import Settings

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/playintegrity"
_ASSERTION_TTL_SECONDS = 3600
_TOKEN_REFRESH_MARGIN_SECONDS = 60

# Google's guidance is not to cache integrity verdicts for licensing. This is
# the tolerance for clock skew and round-trip time, not a cache window.
MAX_VERDICT_AGE_SECONDS = 120


class IntegrityError(Exception):
    """The token could not be decoded, or did not describe a device we trust."""


@dataclass(frozen=True)
class IntegrityVerdict:
    """The subset of the verdict a licensing decision depends on."""

    package_name: str
    app_recognised: bool
    """appIntegrity is PLAY_RECOGNIZED — an unmodified build from Play."""
    device_integrity: frozenset[str]
    """e.g. MEETS_DEVICE_INTEGRITY, MEETS_STRONG_INTEGRITY."""
    account_licensed: bool
    """The account holds an entitlement for this app."""
    request_hash: str | None
    verdict_age_seconds: float

    @property
    def meets_device_integrity(self) -> bool:
        return "MEETS_DEVICE_INTEGRITY" in self.device_integrity

    @property
    def meets_strong_integrity(self) -> bool:
        """StrongBox-class hardware with a locked bootloader and current patches."""
        return "MEETS_STRONG_INTEGRITY" in self.device_integrity


class PlayIntegrityClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._token: str | None = None
        self._token_expires_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(
            self._settings.play_service_account_file
            or self._settings.play_service_account_json
        )

    def _credentials(self) -> dict[str, Any]:
        raw = self._settings.play_service_account_json
        if not raw and self._settings.play_service_account_file:
            try:
                with open(self._settings.play_service_account_file, encoding="utf-8") as handle:
                    raw = handle.read()
            except OSError as exc:
                raise IntegrityError(f"Service account file unreadable: {exc}") from exc
        if not raw:
            raise IntegrityError(
                "Play Integrity is not configured "
                "(set PLAY_SERVICE_ACCOUNT_FILE or PLAY_SERVICE_ACCOUNT_JSON)."
            )
        try:
            creds = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise IntegrityError(f"Service account JSON is not valid JSON: {exc}") from exc
        for field in ("client_email", "private_key"):
            if not creds.get(field):
                raise IntegrityError(f"Service account JSON is missing {field}.")
        return creds

    async def _access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at - _TOKEN_REFRESH_MARGIN_SECONDS:
            return self._token

        creds = self._credentials()
        issued = int(now)
        assertion = jwt.encode(
            {
                "iss": creds["client_email"],
                "scope": _SCOPE,
                "aud": _TOKEN_URL,
                "iat": issued,
                "exp": issued + _ASSERTION_TTL_SECONDS,
            },
            creds["private_key"],
            algorithm="RS256",
        )

        try:
            response = await self._client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                timeout=self._settings.play_api_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise IntegrityError(f"Google token endpoint unreachable: {exc}") from exc

        if response.status_code >= 300:
            raise IntegrityError(
                f"Google token endpoint returned {response.status_code}: {response.text[:300]}"
            )

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise IntegrityError("Google token endpoint returned no access_token.")
        self._token = str(token)
        self._token_expires_at = now + float(payload.get("expires_in", 3600))
        return self._token

    async def decode(self, integrity_token: str) -> IntegrityVerdict:
        """Ask Google to decode the token, and normalise the verdict.

        Never trusts the token's contents without this round trip: a token is
        only meaningful once Google confirms it was issued for our package.
        """
        package = self._settings.play_package_name
        access = await self._access_token()
        url = f"https://playintegrity.googleapis.com/v1/{package}:decodeIntegrityToken"

        try:
            response = await self._client.post(
                url,
                headers={"Authorization": f"Bearer {access}"},
                json={"integrity_token": integrity_token},
                timeout=self._settings.play_api_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise IntegrityError(f"Play Integrity unreachable: {exc}") from exc

        if response.status_code >= 300:
            raise IntegrityError(
                f"Play Integrity returned {response.status_code}: {response.text[:300]}"
            )

        payload = response.json().get("tokenPayloadExternal") or {}
        return _parse_verdict(payload)


def _parse_verdict(payload: dict[str, Any]) -> IntegrityVerdict:
    request_details = payload.get("requestDetails") or {}
    app_integrity = payload.get("appIntegrity") or {}
    device_integrity = payload.get("deviceIntegrity") or {}
    account_details = payload.get("accountDetails") or {}

    # No usable timestamp means freshness cannot be established, and an
    # unbounded age is the safe reading: the caller rejects anything older than
    # MAX_VERDICT_AGE_SECONDS, so an absent or malformed field fails closed
    # rather than passing a replayed verdict.
    timestamp_ms = request_details.get("timestampMillis")
    age = float("inf")
    if timestamp_ms is not None:
        try:
            age = time.time() - (float(timestamp_ms) / 1000.0)
        except (TypeError, ValueError):
            age = float("inf")

    return IntegrityVerdict(
        package_name=str(
            request_details.get("requestPackageName")
            or app_integrity.get("packageName")
            or ""
        ),
        app_recognised=app_integrity.get("appRecognitionVerdict") == "PLAY_RECOGNIZED",
        device_integrity=frozenset(device_integrity.get("deviceRecognitionVerdict") or []),
        account_licensed=account_details.get("appLicensingVerdict") == "LICENSED",
        request_hash=request_details.get("requestHash"),
        verdict_age_seconds=age,
    )


def check_verdict(
    verdict: IntegrityVerdict,
    *,
    expected_package: str,
    expected_request_hash: str,
    require_strong_integrity: bool = False,
) -> None:
    """Raise unless the verdict licenses this request, on this device, now.

    Ordered cheapest-first, and every failure names only what went wrong at a
    level the client can act on — a caller learns that its device failed, not
    which signal Google used, which would be a map for whoever is probing.
    """
    if verdict.package_name != expected_package:
        raise IntegrityError("The integrity token was issued for a different app.")

    if verdict.verdict_age_seconds > MAX_VERDICT_AGE_SECONDS:
        raise IntegrityError("The integrity verdict is stale; request a new one.")

    # The binding that makes replay pointless. Without it, any genuine verdict
    # — including one an attacker obtained legitimately on their own device —
    # could be presented against any licence request.
    if not verdict.request_hash or verdict.request_hash != expected_request_hash:
        raise IntegrityError("The integrity token does not match this request.")

    if not verdict.app_recognised:
        raise IntegrityError("This build was not recognised by Google Play.")

    if require_strong_integrity:
        if not verdict.meets_strong_integrity:
            raise IntegrityError("This device does not meet strong integrity.")
    elif not verdict.meets_device_integrity:
        raise IntegrityError("This device did not pass integrity checks.")
