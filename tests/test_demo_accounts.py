"""Store-review demo accounts.

A billing bypass is exactly the kind of thing that quietly widens, so these
tests pin both halves: that a configured demo address gets the product without
paying, and that nothing else does.
"""

import httpx
import pytest

from app.main import create_app
from tests.conftest import CapturingEmailSender, email_sender, make_settings

DEMO_EMAIL = "admin@creepy.im"
DEMO_NAME = "Admin349401"
DEMO_ENTRY = f"{DEMO_EMAIL}:{DEMO_NAME}"


def _settings_with_demo(tmp_path, demo: str):
    settings = make_settings(tmp_path)
    settings.demo_accounts = demo
    # The real deployment must keep these disjoint; the default fixture makes
    # admin@creepy.im an admin, which would confuse what is being tested here.
    settings.admin_emails = "someone-else@creepy.im"
    return settings


@pytest.fixture
async def demo_client(tmp_path):
    app = create_app(_settings_with_demo(tmp_path, DEMO_ENTRY))
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
    """Sign in, whichever way this account signs in.

    A demo address is auto-verified by request-code and never sees a challenge;
    everyone else goes through the real two-step flow. Written to handle both
    so the same helper serves the "is exempt" and "is not exempt" tests.
    """
    resp = await client.post(
        "/auth/email/request-code",
        json={"email": email, "name": DEMO_NAME, "purpose": "registration"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    if body.get("autoVerified"):
        return {"Authorization": f"Bearer {body['accessToken']}"}

    # Finish the challenge already started above rather than asking for a
    # second code, which would trip the resend cooldown.
    verify = await client.post(
        "/auth/email/verify-code",
        json={
            "challengeId": body["challengeId"],
            "code": email_sender(client).pop_code(email),
            "email": email,
        },
    )
    assert verify.status_code == 200, verify.text
    return {"Authorization": f"Bearer {verify.json()['accessToken']}"}


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


# --- signing in without a code -------------------------------------------


async def test_demo_account_signs_in_without_a_code(demo_client):
    """The whole point: a reviewer cannot read the mailbox, so there is no code.

    request-code returns a usable session and no challenge, and no mail is
    sent — the sender would have recorded it.
    """
    resp = await demo_client.post(
        "/auth/email/request-code",
        json={"email": DEMO_EMAIL, "name": DEMO_NAME, "purpose": "registration"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["autoVerified"] is True
    assert body["accessToken"]
    assert body["refreshToken"]
    assert body["challengeId"] == ""

    sender = demo_client._transport.app.state.email_sender
    assert sender.sent == [], "a demo sign-in must not send mail"

    # The token works.
    me = await demo_client.get(
        "/users/me", headers={"Authorization": f"Bearer {body['accessToken']}"}
    )
    assert me.status_code == 200, me.text
    assert me.json()["email"] == DEMO_EMAIL


async def test_demo_sign_in_is_repeatable(demo_client):
    """No resend cooldown: a reviewer relaunching the app must not hit a wall,
    and the second call must reuse the account rather than making another."""
    first = await demo_client.post(
        "/auth/email/request-code",
        json={"email": DEMO_EMAIL, "name": DEMO_NAME, "purpose": "login"},
    )
    second = await demo_client.post(
        "/auth/email/request-code",
        json={"email": DEMO_EMAIL, "name": DEMO_NAME, "purpose": "login"},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["autoVerified"] is True

    def user_id(resp):
        headers = {"Authorization": f"Bearer {resp.json()['accessToken']}"}
        return headers

    a = await demo_client.get("/users/me", headers=user_id(first))
    b = await demo_client.get("/users/me", headers=user_id(second))
    assert a.json()["userId"] == b.json()["userId"]


async def test_ordinary_accounts_still_get_a_code(demo_client):
    """The bypass is the one address. Everyone else gets the real flow."""
    resp = await demo_client.post(
        "/auth/email/request-code",
        json={"email": "someone@creepy.im", "name": "Someone", "purpose": "registration"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["autoVerified"] is False
    assert body["accessToken"] is None
    assert body["challengeId"]

    sender = demo_client._transport.app.state.email_sender
    assert len(sender.sent) == 1


async def test_no_auto_sign_in_when_unconfigured(no_demo_client):
    """With DEMO_ACCOUNTS empty the same address gets an ordinary code.

    Without this, a bypass that defaulted on would hand a session to anyone who
    typed the address.
    """
    resp = await no_demo_client.post(
        "/auth/email/request-code",
        json={"email": DEMO_EMAIL, "name": DEMO_NAME, "purpose": "registration"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["autoVerified"] is False
    assert resp.json()["accessToken"] is None
    assert len(no_demo_client._transport.app.state.email_sender.sent) == 1


# --- the name is half the credential -------------------------------------


def test_the_name_must_match_exactly(tmp_path):
    from app.db.models import User
    from app.services.entitlements import is_demo_account

    settings = _settings_with_demo(tmp_path, DEMO_ENTRY)

    def account(name):
        return User(user_id="u", email=DEMO_EMAIL, name=name)

    assert is_demo_account(settings, account(DEMO_NAME))
    # Exactly: not a different case, not a prefix, not empty.
    assert not is_demo_account(settings, account("admin349401"))
    assert not is_demo_account(settings, account("ADMIN349401"))
    assert not is_demo_account(settings, account("Admin349401 "))
    assert not is_demo_account(settings, account("Admin"))
    assert not is_demo_account(settings, account(""))
    assert not is_demo_account(settings, account(None))


async def test_wrong_name_gets_the_ordinary_code_flow(demo_client):
    """The right address with the wrong name is just another sign-up.

    It must not error differently either — a distinct response would confirm
    the address to whoever is guessing.
    """
    resp = await demo_client.post(
        "/auth/email/request-code",
        json={"email": DEMO_EMAIL, "name": "Someone Else", "purpose": "registration"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["autoVerified"] is False
    assert body["accessToken"] is None
    assert body["challengeId"]
    # A code was actually sent, exactly as for any other address.
    assert len(demo_client._transport.app.state.email_sender.sent) == 1


async def test_missing_name_gets_the_ordinary_code_flow(demo_client):
    """The sign-in screen sends no name, so it cannot trigger the bypass."""
    resp = await demo_client.post(
        "/auth/email/request-code",
        json={"email": DEMO_EMAIL, "purpose": "login"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["autoVerified"] is False
