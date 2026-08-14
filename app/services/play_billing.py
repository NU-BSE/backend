"""Google Play Billing verification.

A purchase token from the client is a *claim*, not an entitlement. Play is the
only authority on whether money moved, so every token is resolved against the
Android Publisher API before anything is written, and Real-time developer
notifications (RTDN) re-resolve it for the rest of the subscription's life —
renewals, cancellations, refunds, grace periods and revocations all arrive that
way rather than from the app.

The client never tells us the plan, the price or the expiry. It sends a token;
everything else is read from Google's response. A client that could name its
own plan could name the free trial forever.

Authentication is a service-account JWT assertion exchanged for an access
token, done over httpx rather than google-api-python-client: that library is
synchronous and would block the event loop on every call, and this needs three
endpoints.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import jwt

from app.core.config import Settings

logger = logging.getLogger("app.play")

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
_API_ROOT = "https://androidpublisher.googleapis.com/androidpublisher/v3"

# Assertions are short-lived by design; Google rejects anything over an hour.
_ASSERTION_TTL_SECONDS = 3600
# Renew a little early so an in-flight call cannot land on an expired token.
_TOKEN_REFRESH_MARGIN_SECONDS = 60


class PlayError(Exception):
    """Play could not be reached, or answered in a way we will not act on."""


class PlayPurchaseInvalid(PlayError):
    """Play answered, and the answer is that this purchase confers nothing."""


@dataclass(frozen=True)
class PlaySubscription:
    """The parts of Play's subscription state this service acts on."""

    purchase_token: str
    product_id: str
    base_plan_id: str | None
    # Play's own vocabulary, normalized to the subscriptions.status CHECK set.
    status: str
    expiry: datetime
    start: datetime
    auto_renewing: bool
    acknowledged: bool
    in_trial: bool
    # Set when Play replaced this token (an upgrade/downgrade); the old token
    # stops renewing and must not keep an entitlement alive.
    linked_purchase_token: str | None


# Play's SubscriptionState -> the status vocabulary in sql/migrate_001.sql.
#
# PENDING is deliberately absent: a purchase awaiting payment (slow card, cash
# at a kiosk) has not paid, and mapping it to anything active would hand out
# the product on a promise. It raises PlayPurchaseInvalid instead.
_STATE_TO_STATUS: dict[str, str] = {
    "SUBSCRIPTION_STATE_ACTIVE": "active",
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD": "past_due",
    "SUBSCRIPTION_STATE_ON_HOLD": "past_due",
    "SUBSCRIPTION_STATE_PAUSED": "past_due",
    "SUBSCRIPTION_STATE_CANCELED": "canceled",
    "SUBSCRIPTION_STATE_EXPIRED": "expired",
}

# States that still entitle the user. Cancelled is included on purpose: Play
# reports CANCELED as soon as auto-renew is switched off, while the user has
# already paid through the end of the period and keeps access until then.
ENTITLING_STATUSES = frozenset({"active", "trialing", "past_due", "canceled"})


class PlayClient:
    """Android Publisher access with a cached service-account token."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._token: str | None = None
        self._token_expires_at: float = 0.0

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
                raise PlayError(f"Play service account file unreadable: {exc}") from exc
        if not raw:
            raise PlayError(
                "Google Play is not configured "
                "(set PLAY_SERVICE_ACCOUNT_FILE or PLAY_SERVICE_ACCOUNT_JSON)."
            )
        try:
            creds = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlayError(f"Play service account JSON is not valid JSON: {exc}") from exc
        for field in ("client_email", "private_key"):
            if not creds.get(field):
                raise PlayError(f"Play service account JSON is missing {field}.")
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
            raise PlayError(f"Google token endpoint unreachable: {exc}") from exc

        if response.status_code >= 300:
            raise PlayError(
                f"Google token endpoint returned {response.status_code}: "
                f"{response.text[:300]}"
            )

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise PlayError("Google token endpoint returned no access_token.")
        self._token = str(token)
        self._token_expires_at = now + float(payload.get("expires_in", 3600))
        return self._token

    async def _get(self, path: str) -> dict[str, Any]:
        token = await self._access_token()
        try:
            response = await self._client.get(
                f"{_API_ROOT}{path}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._settings.play_api_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise PlayError(f"Android Publisher unreachable: {exc}") from exc

        if response.status_code == 404:
            raise PlayPurchaseInvalid("Play does not recognise this purchase token.")
        if response.status_code == 410:
            raise PlayPurchaseInvalid("This purchase token is no longer valid.")
        if response.status_code >= 300:
            raise PlayError(
                f"Android Publisher returned {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    async def get_subscription(self, purchase_token: str) -> PlaySubscription:
        """Resolve a purchase token to Play's current view of it."""
        package = self._settings.play_package_name
        payload = await self._get(
            f"/applications/{package}/purchases/subscriptionsv2/tokens/{purchase_token}"
        )
        return _parse_subscription(purchase_token, payload)

    async def acknowledge(self, product_id: str, purchase_token: str) -> None:
        """Tell Play the entitlement was delivered.

        Play refunds any purchase that is not acknowledged within three days,
        so this is not optional bookkeeping — skipping it silently revokes the
        user's subscription and their money comes back.
        """
        token = await self._access_token()
        package = self._settings.play_package_name
        url = (
            f"{_API_ROOT}/applications/{package}/purchases/subscriptions/"
            f"{product_id}/tokens/{purchase_token}:acknowledge"
        )
        try:
            response = await self._client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={},
                timeout=self._settings.play_api_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise PlayError(f"Android Publisher unreachable: {exc}") from exc

        # 400 with alreadyAcknowledged is success from where we stand: the goal
        # is that Play considers it acknowledged, not that we were the one to
        # do it. Retries after a partial failure must not blow up here.
        if response.status_code < 300:
            return
        if response.status_code == 400 and "already" in response.text.lower():
            return
        raise PlayError(
            f"Acknowledge failed with {response.status_code}: {response.text[:300]}"
        )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    # Play returns nanoseconds; datetime handles at most microseconds.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())[:6]
        offset = tail[len(digits) :].lstrip("0123456789")
        text = f"{head}.{digits or '0'}{offset}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_subscription(purchase_token: str, payload: dict[str, Any]) -> PlaySubscription:
    state = str(payload.get("subscriptionState", ""))

    if state == "SUBSCRIPTION_STATE_PENDING":
        raise PlayPurchaseInvalid(
            "This purchase has not been paid for yet. Play will notify us when it completes."
        )
    status = _STATE_TO_STATUS.get(state)
    if status is None:
        raise PlayPurchaseInvalid(f"Unhandled Play subscription state: {state or 'missing'}")

    line_items = payload.get("lineItems")
    if not isinstance(line_items, list) or not line_items:
        raise PlayPurchaseInvalid("Play returned a subscription with no line items.")

    # A subscription can carry several line items during an upgrade. The one
    # that expires last is the one the user actually holds.
    def expiry_of(item: Any) -> datetime:
        parsed = _parse_time((item or {}).get("expiryTime"))
        return parsed or datetime.fromtimestamp(0, tz=timezone.utc)

    item = max(line_items, key=expiry_of)
    expiry = expiry_of(item)
    if expiry <= datetime.fromtimestamp(0, tz=timezone.utc):
        raise PlayPurchaseInvalid("Play returned a subscription with no expiry time.")

    offer = item.get("offerDetails") or {}
    in_trial = "autoRenewingPlan" in item and bool(
        (item.get("autoRenewingPlan") or {}).get("priceChangeDetails") is None
        and payload.get("testPurchase") is None
        and _has_trial(item)
    )

    return PlaySubscription(
        purchase_token=purchase_token,
        product_id=str(item.get("productId") or ""),
        base_plan_id=(str(offer.get("basePlanId")) if offer.get("basePlanId") else None),
        # Trials are active subscriptions in Play's model but a distinct status
        # in ours, so the gate can tell "paid" from "has not paid yet".
        status="trialing" if (status == "active" and in_trial) else status,
        expiry=expiry,
        start=_parse_time(payload.get("startTime")) or datetime.now(timezone.utc),
        auto_renewing=bool((item.get("autoRenewingPlan") or {}).get("autoRenewEnabled")),
        acknowledged=str(payload.get("acknowledgementState", ""))
        == "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
        in_trial=in_trial,
        linked_purchase_token=(
            str(payload["linkedPurchaseToken"])
            if payload.get("linkedPurchaseToken")
            else None
        ),
    )


def _has_trial(item: dict[str, Any]) -> bool:
    offer = item.get("offerDetails") or {}
    offer_tags = offer.get("offerTags")
    if isinstance(offer_tags, list) and any(
        "trial" in str(tag.get("tag", "")).lower() for tag in offer_tags if isinstance(tag, dict)
    ):
        return True
    # Play does not always tag the trial, but a free-trial period always shows
    # up as a zero-price recurrence on the line item.
    price = (item.get("autoRenewingPlan") or {}).get("recurringPrice") or {}
    return str(price.get("units", "")) == "0" and int(price.get("nanos", 0) or 0) == 0


@dataclass(frozen=True)
class RtdnNotification:
    """The subscription half of a decoded RTDN envelope."""

    package_name: str
    purchase_token: str
    product_id: str
    notification_type: int


# https://developer.android.com/google/play/billing/rtdn-reference
RTDN_SUBSCRIPTION_REVOKED = 12
RTDN_SUBSCRIPTION_EXPIRED = 13


def decode_rtdn(body: dict[str, Any]) -> RtdnNotification | None:
    """Decode a Pub/Sub push envelope into a subscription notification.

    Returns None for envelopes this service does not act on — test pings and
    one-time-product or voided-purchase notifications — so the caller can
    still answer 200 and stop Pub/Sub retrying something it will never accept.
    """
    message = body.get("message")
    if not isinstance(message, dict):
        return None
    data = message.get("data")
    if not isinstance(data, str) or not data:
        return None
    try:
        decoded = json.loads(base64.b64decode(data).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning("undecodable RTDN payload: %s", exc)
        return None

    notification = decoded.get("subscriptionNotification")
    if not isinstance(notification, dict):
        return None
    purchase_token = notification.get("purchaseToken")
    if not purchase_token:
        return None

    return RtdnNotification(
        package_name=str(decoded.get("packageName") or ""),
        purchase_token=str(purchase_token),
        product_id=str(notification.get("subscriptionId") or ""),
        notification_type=int(notification.get("notificationType") or 0),
    )
