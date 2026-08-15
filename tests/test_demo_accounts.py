"""Store-review demo accounts.

A billing bypass is exactly the kind of thing that quietly widens, so these
tests pin both halves: that a configured demo address gets the product without
paying, and that nothing else does.
"""

import httpx
import pytest

from app.main import create_app
from tests.conftest import CapturingEmailSender, make_settings, register_user

DEMO_EMAIL = "admin@creepy.im"


def _settings_with_demo(tmp_path, demo: str):
    settings = make_settings(tmp_path)
    settings.demo_accounts = demo
    # The real deployment must keep these disjoint; the default fixture makes
    # admin@creepy.im an admin, which would confuse what is being tested here.
    settings.admin_emails = "someone-else@creepy.im"
    return settings


@pytest.fixture
async def demo_client(tmp_path):
    app = create_app(_settings_with_demo(tmp_path, DEMO_EMAIL))
    async with app.router.lifespan_context(app):
        app.state.email_sender = CapturingEmailSender()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
async def no_demo_client(tmp_path):
    app = create_app(_settings_with_demo(tmp_path, ""))
    async with app.router.lifespan_context(app):
        app.state.email_sender = CapturingEmailSender()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _auth(client, email: str) -> dict[str, str]:
    tokens = await register_user(client, email=email, name="Admin349401")
    return {"Authorization": f"Bearer {tokens['accessToken']}"}


async def test_demo_account_is_entitled_without_paying(demo_client):
    headers = await _auth(demo_client, DEMO_EMAIL)

    resp = await demo_client.get("/subscriptions/me", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["entitlements"]["agentAccess"] is True
    assert body["entitlements"]["cloudAgentAllowed"] is True
    assert body["entitlements"]["subscriptionRequired"] is False
    assert body["entitlements"]["planCode"] == "demo"

    # No *paid* subscription is fabricated. Registration grants everyone the
    # free plan, and that row is left exactly as it is: the exemption lives in
    # configuration, so removing the address revokes it at once and leaves no
    # orphaned "active" subscription nobody ever paid for.
    assert body["subscription"]["planId"] == "plan_free"


async def test_demo_account_reaches_the_cloud_agent(demo_client):
    headers = await _auth(demo_client, DEMO_EMAIL)
    resp = await demo_client.post(
        "/chat/http",
        json={"messages": [{"id": "m1", "role": "user", "content": "hello"}]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


async def test_demo_account_may_download_model_weights(demo_client):
    headers = await _auth(demo_client, DEMO_EMAIL)
    resp = await demo_client.get("/models/catalog", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["downloadAllowed"] is True


def test_matching_is_case_and_space_insensitive(tmp_path):
    """Tested directly: the auth layer lowercases before this is ever reached,
    so an end-to-end case test would only prove that auth normalizes. What
    matters here is that a sloppily configured DEMO_ACCOUNTS still matches."""
    from app.db.models import User
    from app.services.entitlements import is_demo_account

    settings = _settings_with_demo(tmp_path, " Admin@Creepy.IM , spare@creepy.im ")

    assert is_demo_account(settings, User(user_id="u", email="admin@creepy.im"))
    assert is_demo_account(settings, User(user_id="u", email="ADMIN@CREEPY.IM"))
    assert is_demo_account(settings, User(user_id="u", email="spare@creepy.im"))
    assert not is_demo_account(settings, User(user_id="u", email="other@creepy.im"))
    assert not is_demo_account(settings, User(user_id="u", email=None))
    assert not is_demo_account(settings, None)

    # Empty configuration matches nothing, including the empty string.
    off = _settings_with_demo(tmp_path, "")
    assert not is_demo_account(off, User(user_id="u", email="admin@creepy.im"))
    assert not is_demo_account(off, User(user_id="u", email=""))


async def test_other_accounts_still_have_to_pay(demo_client):
    """The bypass is one address, not a mood the server is in."""
    headers = await _auth(demo_client, "someone@creepy.im")

    resp = await demo_client.get("/subscriptions/me", headers=headers)
    body = resp.json()
    assert body["entitlements"]["cloudAgentAllowed"] is False
    assert body["entitlements"]["subscriptionRequired"] is True

    denied = await demo_client.get(
        "/models/efficient/files/anything.gguf", headers=headers
    )
    assert denied.status_code in (402, 404)


async def test_bypass_is_off_unless_configured(no_demo_client):
    """The same address gets nothing when DEMO_ACCOUNTS is empty.

    This is the important one. If the bypass ever defaults on, the paid
    product is free to anyone who guesses the address.
    """
    headers = await _auth(no_demo_client, DEMO_EMAIL)

    resp = await no_demo_client.get("/subscriptions/me", headers=headers)
    body = resp.json()
    assert body["entitlements"]["cloudAgentAllowed"] is False
    assert body["entitlements"]["subscriptionRequired"] is True

    denied = await no_demo_client.post(
        "/chat/http",
        json={"messages": [{"id": "m1", "role": "user", "content": "hello"}]},
        headers=headers,
    )
    assert denied.status_code == 403


async def test_demo_account_is_not_an_admin(demo_client):
    """Credentials handed to reviewers must not reach /admin/grant."""
    headers = await _auth(demo_client, DEMO_EMAIL)
    resp = await demo_client.post(
        "/admin/grant",
        json={"userId": "usr_x", "planCode": "pro", "days": 30},
        headers=headers,
    )
    assert resp.status_code == 403
