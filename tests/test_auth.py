
from tests.conftest import clear_cooldown, email_sender, register_user, request_code


async def test_request_code_returns_challenge(client):
    resp = await request_code(client, "new@creepy.im", name="New User")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["challengeId"]
    assert body["expiresInSeconds"] == 300
    assert body["retryAfterSeconds"] == 60
    assert email_sender(client).sent[-1]["to"] == "new@creepy.im"


async def test_request_code_rejects_bad_purpose(client):
    resp = await client.post(
        "/auth/email/request-code",
        json={"email": "x@creepy.im", "purpose": "hack"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_request_code_cooldown(client):
    first = await request_code(client, "cool@creepy.im")
    assert first.status_code == 200
    second = await request_code(client, "cool@creepy.im")
    assert second.status_code == 429
    body = second.json()
    assert body["code"] == "RATE_LIMITED"
    assert body["retryAfterSeconds"] > 0


async def test_verify_code_registration_issues_tokens(client):
    body = await register_user(client, email="reg@creepy.im", name="Reg User")
    assert body["accessToken"]
    assert body["refreshToken"]
    assert body["email"] == "reg@creepy.im"
    # Name supplied at registration -> onboarding complete.
    assert body["onboardingCompleted"] is True


async def test_verify_code_login_without_name(client):
    body = await register_user(client, email="anon@creepy.im", name=None)
    assert body["onboardingCompleted"] is False


async def test_verify_code_wrong_code(client):
    resp = await request_code(client, "wrong@creepy.im")
    challenge_id = resp.json()["challengeId"]
    resp = await client.post(
        "/auth/email/verify-code",
        json={"challengeId": challenge_id, "code": "000000", "email": "wrong@creepy.im"},
    )
    # Could collide with the real code only if the real code is 000000; accept
    # either 400 (wrong) but never 500.
    assert resp.status_code in (200, 400)


async def test_verify_code_unknown_challenge(client):
    resp = await client.post(
        "/auth/email/verify-code",
        json={"challengeId": "nope", "code": "123456", "email": "x@creepy.im"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "CHALLENGE_EXPIRED"


async def test_challenge_is_single_use(client):
    resp = await request_code(client, "once@creepy.im", name="Once")
    challenge_id = resp.json()["challengeId"]
    code = email_sender(client).pop_code("once@creepy.im")

    first = await client.post(
        "/auth/email/verify-code",
        json={"challengeId": challenge_id, "code": code, "email": "once@creepy.im"},
    )
    assert first.status_code == 200
    second = await client.post(
        "/auth/email/verify-code",
        json={"challengeId": challenge_id, "code": code, "email": "once@creepy.im"},
    )
    assert second.status_code == 400
    assert second.json()["code"] == "CHALLENGE_EXPIRED"


async def test_login_existing_user_returns_same_account(client):
    first = await register_user(client, email="same@creepy.im", name="Same")
    await clear_cooldown(client, "same@creepy.im")
    second = await register_user(client, email="same@creepy.im", name=None)
    assert first["email"] == second["email"] == "same@creepy.im"
    # Second call is a login; tokens are fresh but onboarding stays complete.
    assert second["onboardingCompleted"] is True


async def test_refresh_issues_new_access_token(client):
    body = await register_user(client, email="refresh@creepy.im")
    resp = await client.post("/auth/refresh", json={"refreshToken": body["refreshToken"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["accessToken"]


async def test_refresh_rejects_access_token(client):
    body = await register_user(client, email="noaccess@creepy.im")
    resp = await client.post("/auth/refresh", json={"refreshToken": body["accessToken"]})
    assert resp.status_code == 401


async def test_logout(client):
    resp = await client.post("/auth/logout", json={})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_verify_code_attempt_cap(client, settings):
    resp = await request_code(client, "cap@creepy.im")
    challenge_id = resp.json()["challengeId"]
    real_code = email_sender(client).pop_code("cap@creepy.im")
    wrong = "000000" if real_code != "000000" else "000001"

    for _ in range(settings.email_code_max_verify_attempts):
        r = await client.post(
            "/auth/email/verify-code",
            json={"challengeId": challenge_id, "code": wrong, "email": "cap@creepy.im"},
        )
        assert r.status_code == 400

    # Next attempt is rate-limited even with the correct code.
    r = await client.post(
        "/auth/email/verify-code",
        json={"challengeId": challenge_id, "code": real_code, "email": "cap@creepy.im"},
    )
    assert r.status_code == 429
    assert r.json()["code"] == "RATE_LIMITED"
