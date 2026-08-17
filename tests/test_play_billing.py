"""Google Play purchase verification.

The client is not trusted anywhere in these tests: every case asserts that what
lands in the database came from Play's answer, not from the request body.
"""

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services.play_billing import (
    PlayPurchaseInvalid,
    PlaySubscription,
    _parse_subscription,
    decode_rtdn,
)
from tests.conftest import register_user


def _play_payload(
    *,
    state: str = "SUBSCRIPTION_STATE_ACTIVE",
    base_plan_id: str = "plan-1",
    expiry_days: int = 30,
    price_units: str = "12",
    acknowledged: bool = False,
    linked: str | None = None,
) -> dict:
    expiry = datetime.now(timezone.utc) + timedelta(days=expiry_days)
    payload = {
        "subscriptionState": state,
        "startTime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "acknowledgementState": (
            "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED"
            if acknowledged
            else "ACKNOWLEDGEMENT_STATE_PENDING"
        ),
        "lineItems": [
            {
                "productId": "creepyim_pro",
                "expiryTime": expiry.isoformat().replace("+00:00", "Z"),
                "offerDetails": {"basePlanId": base_plan_id},
                "autoRenewingPlan": {
                    "autoRenewEnabled": True,
                    "recurringPrice": {"units": price_units, "nanos": 0},
                },
            }
        ],
    }
    if linked:
        payload["linkedPurchaseToken"] = linked
    return payload


class FakePlayClient:
    """Stands in for Android Publisher. Records what it was asked."""

    def __init__(self, payload: dict | None = None, error: Exception | None = None):
        self._payload = payload if payload is not None else _play_payload()
        self._error = error
        self.acknowledged: list[tuple[str, str]] = []
        self.configured = True

    async def get_subscription(self, purchase_token: str) -> PlaySubscription:
        if self._error:
            raise self._error
        return _parse_subscription(purchase_token, self._payload)

    async def acknowledge(self, product_id: str, purchase_token: str) -> None:
        self.acknowledged.append((product_id, purchase_token))


async def _auth(client, email: str) -> dict[str, str]:
    tokens = await register_user(client, email=email)
    return {"Authorization": f"Bearer {tokens['accessToken']}"}


def _install(client, fake: FakePlayClient) -> None:
    client._transport.app.state.play_client = fake


# --- parsing -------------------------------------------------------------


def test_pending_purchase_is_refused():
    """A purchase awaiting payment has not paid for anything."""
    with pytest.raises(PlayPurchaseInvalid):
        _parse_subscription("tok", _play_payload(state="SUBSCRIPTION_STATE_PENDING"))


def test_zero_price_line_item_is_a_trial():
    parsed = _parse_subscription("tok", _play_payload(price_units="0"))
    assert parsed.in_trial is True
    assert parsed.status == "trialing"


def test_cancelled_keeps_its_paid_period():
    """Auto-renew off is not the same as access off."""
    parsed = _parse_subscription("tok", _play_payload(state="SUBSCRIPTION_STATE_CANCELED"))
    assert parsed.status == "canceled"
    assert parsed.expiry > datetime.now(timezone.utc)


def test_nanosecond_timestamps_parse():
    payload = _play_payload()
    payload["lineItems"][0]["expiryTime"] = "2030-01-01T00:00:00.123456789Z"
    parsed = _parse_subscription("tok", payload)
    assert parsed.expiry.year == 2030


def test_longest_line_item_wins():
    payload = _play_payload()
    payload["lineItems"].insert(
        0,
        {
            "productId": "creepyim_pro",
            "expiryTime": "2020-01-01T00:00:00Z",
            "offerDetails": {"basePlanId": "plan-1"},
        },
    )
    parsed = _parse_subscription("tok", payload)
    assert parsed.expiry.year > 2020


def test_decode_rtdn_ignores_test_pings():
    encoded = base64.b64encode(json.dumps({"testNotification": {"version": "1"}}).encode())
    assert decode_rtdn({"message": {"data": encoded.decode()}}) is None


def test_decode_rtdn_extracts_subscription():
    payload = {
        "packageName": "im.creepy.app",
        "subscriptionNotification": {
            "purchaseToken": "tok-1",
            "subscriptionId": "creepyim_pro",
            "notificationType": 2,
        },
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    decoded = decode_rtdn({"message": {"data": encoded}})
    assert decoded is not None
    assert decoded.purchase_token == "tok-1"
    assert decoded.notification_type == 2


# --- the endpoint --------------------------------------------------------


async def test_verify_grants_entitlement(client):
    headers = await _auth(client, "buyer@creepy.im")
    fake = FakePlayClient()
    _install(client, fake)

    resp = await client.post(
        "/subscriptions/play/verify", json={"purchaseToken": "tok-1"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entitlements"]["cloudAgentAllowed"] is True
    assert body["entitlements"]["planCode"] == "pro"
    assert body["subscription"]["provider"] == "google_play"
    # Acknowledged, or Play refunds it after three days.
    assert fake.acknowledged == [("creepyim_pro", "tok-1")]


async def test_annual_base_plan_maps_to_annual_plan(client):
    headers = await _auth(client, "annual@creepy.im")
    _install(client, FakePlayClient(_play_payload(base_plan_id="plan-2")))

    resp = await client.post(
        "/subscriptions/play/verify", json={"purchaseToken": "tok-a"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["entitlements"]["planCode"] == "pro_annual"


async def test_client_cannot_choose_its_own_plan(client):
    """The request body names the annual plan; Play says monthly. Play wins."""
    headers = await _auth(client, "liar@creepy.im")
    _install(client, FakePlayClient(_play_payload(base_plan_id="plan-1")))

    resp = await client.post(
        "/subscriptions/play/verify",
        json={"purchaseToken": "tok-x", "basePlanId": "plan-2"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["entitlements"]["planCode"] == "pro"


async def test_unknown_base_plan_is_rejected(client):
    headers = await _auth(client, "unknown@creepy.im")
    _install(client, FakePlayClient(_play_payload(base_plan_id="someone-elses-plan")))

    resp = await client.post(
        "/subscriptions/play/verify", json={"purchaseToken": "tok-u"}, headers=headers
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "PLAY_PURCHASE_INVALID"


async def test_token_cannot_be_reused_by_another_account(client):
    """A purchase token is bearer proof of payment; one token, one account."""
    first = await _auth(client, "first@creepy.im")
    second = await _auth(client, "second@creepy.im")
    _install(client, FakePlayClient())

    assert (
        await client.post(
            "/subscriptions/play/verify", json={"purchaseToken": "shared"}, headers=first
        )
    ).status_code == 200

    resp = await client.post(
        "/subscriptions/play/verify", json={"purchaseToken": "shared"}, headers=second
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "PLAY_PURCHASE_ALREADY_LINKED"


async def test_verify_is_idempotent(client):
    headers = await _auth(client, "twice@creepy.im")
    _install(client, FakePlayClient())

    first = await client.post(
        "/subscriptions/play/verify", json={"purchaseToken": "tok-i"}, headers=headers
    )
    second = await client.post(
        "/subscriptions/play/verify", json={"purchaseToken": "tok-i"}, headers=headers
    )
    assert first.status_code == second.status_code == 200
    assert (
        first.json()["subscription"]["subscriptionId"]
        == second.json()["subscription"]["subscriptionId"]
    )


async def test_expired_purchase_confers_nothing(client):
    headers = await _auth(client, "expired@creepy.im")
    _install(
        client,
        FakePlayClient(
            _play_payload(state="SUBSCRIPTION_STATE_EXPIRED", expiry_days=-1)
        ),
    )

    resp = await client.post(
        "/subscriptions/play/verify", json={"purchaseToken": "tok-e"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["entitlements"]["cloudAgentAllowed"] is False


async def test_verify_requires_authentication(client):
    _install(client, FakePlayClient())
    resp = await client.post("/subscriptions/play/verify", json={"purchaseToken": "t"})
    assert resp.status_code == 401


async def test_rtdn_requires_the_shared_secret(client, settings):
    settings.play_rtdn_secret = "s3cret"
    _install(client, FakePlayClient())
    resp = await client.post("/subscriptions/play/rtdn", json={"message": {"data": ""}})
    assert resp.status_code == 401


async def test_rtdn_applies_a_renewal(client, settings):
    settings.play_rtdn_secret = "s3cret"
    headers = await _auth(client, "renew@creepy.im")
    _install(client, FakePlayClient())

    await client.post(
        "/subscriptions/play/verify", json={"purchaseToken": "tok-r"}, headers=headers
    )

    # Play now reports a later expiry, as it would after a renewal.
    _install(client, FakePlayClient(_play_payload(expiry_days=60)))
    envelope = base64.b64encode(
        json.dumps(
            {
                "packageName": "im.creepy.app",
                "subscriptionNotification": {
                    "purchaseToken": "tok-r",
                    "subscriptionId": "creepyim_pro",
                    "notificationType": 2,
                },
            }
        ).encode()
    ).decode()

    resp = await client.post(
        "/subscriptions/play/rtdn",
        json={"message": {"data": envelope}},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"

    me = await client.get("/subscriptions/me", headers=headers)
    assert me.json()["entitlements"]["cloudAgentAllowed"] is True


async def test_rtdn_revocation_removes_access(client, settings):
    settings.play_rtdn_secret = "s3cret"
    headers = await _auth(client, "refund@creepy.im")
    _install(client, FakePlayClient())
    await client.post(
        "/subscriptions/play/verify", json={"purchaseToken": "tok-v"}, headers=headers
    )

    # A refund: Play now reports the subscription as expired.
    _install(
        client,
        FakePlayClient(_play_payload(state="SUBSCRIPTION_STATE_EXPIRED", expiry_days=-1)),
    )
    envelope = base64.b64encode(
        json.dumps(
            {
                "packageName": "im.creepy.app",
                "subscriptionNotification": {
                    "purchaseToken": "tok-v",
                    "subscriptionId": "creepyim_pro",
                    "notificationType": 12,
                },
            }
        ).encode()
    ).decode()

    resp = await client.post(
        "/subscriptions/play/rtdn",
        json={"message": {"data": envelope}},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert resp.status_code == 200, resp.text

    me = await client.get("/subscriptions/me", headers=headers)
    assert me.json()["entitlements"]["cloudAgentAllowed"] is False
