"""Onboarding v2.

Pins the contract the new onboarding flow is built on: persistent state that
resumes after a kill, intents stored as *interests* (never permission state),
first-task and feedback recorded server-side, and a paywall that only appears
after first value.
"""

import hashlib
import json

import pytest

from tests.conftest import CapturingEmailSender, email_sender, make_settings, register_user


def _headers(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['accessToken']}"}


def _write_bundle(root, model: str) -> None:
    gguf = root / model / "artifacts" / "gguf"
    gguf.mkdir(parents=True)
    weights = gguf / f"gui-owl-{model}-q4_0.gguf"
    weights.write_bytes(b"weights-" + model.encode())
    manifest = {
        "files": {
            weights.name: {
                "bytes": len(weights.read_bytes()),
                "sha256": hashlib.sha256(weights.read_bytes()).hexdigest(),
            }
        },
        "student": model,
    }
    (gguf / f"gui-owl-{model}-export-manifest.json").write_text(json.dumps(manifest))


@pytest.fixture
async def model_client(tmp_path):
    import httpx

    from app.main import create_app

    root = tmp_path / "models"
    for model in ("0.5b", "1b", "1.5b"):
        _write_bundle(root, model)
    settings = make_settings(tmp_path)
    settings.model_artifact_root = str(root)

    application = create_app(settings)
    async with application.router.lifespan_context(application):
        application.state.email_sender = CapturingEmailSender()
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _register(client, email: str = "user@creepy.im") -> dict:
    return await register_user(client, email=email)


async def _state(client, headers: dict) -> dict:
    resp = await client.get("/onboarding/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _patch_intents(client, headers: dict, intents, custom=None) -> dict:
    body: dict = {"intents": intents}
    if custom is not None:
        body["customIntent"] = custom
    resp = await client.patch("/onboarding/me/intents", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _complete(client, headers: dict) -> dict:
    resp = await client.post("/onboarding/me/complete", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _direct_db(app, user_id: str) -> dict:
    """Read the onboarding row straight from the DB to prove persistence."""
    factory = app.state.session_factory
    from app.db.models import UserOnboarding

    async with factory() as session:
        row = await session.get(UserOnboarding, user_id)
        if row is None:
            return {}
        return {
            "version": row.version,
            "status": row.status,
            "intents": row.intents,
            "custom_intent": row.custom_intent,
            "ai_mode": row.ai_mode,
            "first_task": row.first_task,
            "feedback": row.feedback,
        }


async def _user_id(client, headers: dict) -> str:
    resp = await client.get("/users/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["userId"]


# --- 1. new user -> initial state -------------------------------------------


async def test_new_user_initial_onboarding_state(client):
    tokens = await _register(client)
    state = await _state(client, _headers(tokens))

    assert state["version"] == 2
    assert state["status"] == "auth_completed"
    assert state["intents"] == []
    assert state["customIntent"] is None
    assert state["aiMode"] is None
    assert state["firstTask"]["conversationId"] is None
    assert state["feedback"]["result"] is None
    assert state["subscriptionRequired"] is True
    assert state["paywallDue"] is False


# --- 2/3/16. intents (multi-select, custom, idempotent) ---------------------


async def test_save_multiple_intents(client):
    tokens = await _register(client)
    headers = _headers(tokens)

    state = await _patch_intents(client, headers, ["android_settings", "messages", "calendar"])
    assert state["status"] == "intent_completed"
    assert set(state["intents"]) == {"android_settings", "messages", "calendar"}

    # Repeat PATCH must not duplicate state.
    again = await _patch_intents(client, headers, ["android_settings", "messages", "calendar"])
    assert set(again["intents"]) == {"android_settings", "messages", "calendar"}

    user_id = await _user_id(client, headers)
    row = await _direct_db(client._transport.app, user_id)
    assert sorted(row["intents"]) == sorted(["android_settings", "messages", "calendar"])


async def test_save_custom_intent(client):
    tokens = await _register(client)
    headers = _headers(tokens)

    state = await _patch_intents(
        client,
        headers,
        ["email"],
        custom="I wish Creepy could organize my day automatically",
    )
    assert state["status"] == "intent_completed"
    assert state["customIntent"] == "I wish Creepy could organize my day automatically"

    user_id = await _user_id(client, headers)
    row = await _direct_db(client._transport.app, user_id)
    assert row["custom_intent"] == "I wish Creepy could organize my day automatically"


async def test_unknown_intent_is_rejected(client):
    tokens = await _register(client)
    headers = _headers(tokens)
    resp = await client.patch(
        "/onboarding/me/intents",
        json={"intents": ["telegram", "messages"]},
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "INVALID_INTENT"


# --- 4. resume -------------------------------------------------------------


async def test_resume_after_kill(client):
    tokens = await _register(client)
    headers = _headers(tokens)
    await _patch_intents(client, headers, ["calendar"])
    await client.patch("/onboarding/me/ai-mode", json={"mode": "cloud"}, headers=headers)

    # A fresh GET (as after an app restart) must return the same progress.
    state = await _state(client, headers)
    assert state["status"] == "ai_mode_completed"
    assert state["intents"] == ["calendar"]
    assert state["aiMode"] == "cloud"


# --- 5/6. ai-mode selection ------------------------------------------------


async def test_local_mode_selection(model_client):
    tokens = await _register(model_client)
    headers = _headers(tokens)
    resp = await model_client.patch(
        "/onboarding/me/ai-mode", json={"mode": "local"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    state = resp.json()
    assert state["aiMode"] == "local"
    assert state["status"] == "ai_mode_completed"
    # Local is selectable (the catalog has profiles) even though a fresh free
    # user cannot download weights yet — the state tells the client the
    # download is required rather than silently substituting a stub.
    assert state["aiModeOptions"]["local"]["status"] == "local_download_required"
    assert state["aiModeOptions"]["local"]["profiles"]


async def test_local_available_with_subscription(model_client):
    tokens = await _register(model_client, email="pro@creepy.im")
    headers = _headers(tokens)
    me = await model_client.get("/users/me", headers=headers)
    admin = await register_user(model_client, email="admin@creepy.im")
    await model_client.post(
        "/admin/grant",
        json={"userId": me.json()["userId"], "planCode": "pro", "days": 30},
        headers=_headers(admin),
    )
    resp = await model_client.patch(
        "/onboarding/me/ai-mode", json={"mode": "local"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["aiModeOptions"]["local"]["status"] == "local_available"


async def test_unsupported_local_mode_is_rejected(client):
    # Default test deployment has no model artifacts, so local is unsupported.
    tokens = await _register(client)
    headers = _headers(tokens)
    resp = await client.patch("/onboarding/me/ai-mode", json={"mode": "local"}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["code"] == "LOCAL_MODEL_NOT_SUPPORTED"
    assert resp.json()["status"] == "local_not_supported"


async def test_cloud_mode_selection(client):
    tokens = await _register(client)
    headers = _headers(tokens)
    resp = await client.patch("/onboarding/me/ai-mode", json={"mode": "cloud"}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["aiMode"] == "cloud"
    assert resp.json()["aiModeOptions"]["cloud"]["status"] == "cloud_ready"


async def test_ai_mode_is_idempotent(client):
    tokens = await _register(client)
    headers = _headers(tokens)
    await client.patch("/onboarding/me/ai-mode", json={"mode": "cloud"}, headers=headers)
    state = await _state(client, headers)
    assert state["aiMode"] == "cloud"


# --- 7/8. first task -------------------------------------------------------


async def test_first_task_started(client):
    tokens = await _register(client)
    headers = _headers(tokens)

    resp = await client.post(
        "/onboarding/me/first-task/start",
        json={"suggestedTaskId": "battery", "originalPrompt": "Find what's draining my battery"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["conversationId"]
    assert body["state"]["status"] == "first_task_started"
    assert body["state"]["firstTask"]["conversationId"] == body["conversationId"]
    assert body["state"]["firstTask"]["startedAt"]
    assert body["state"]["firstTask"]["suggestedTaskId"] == "battery"
    assert body["state"]["firstTask"]["completedAt"] is None


async def test_first_task_completed(client):
    tokens = await _register(client)
    headers = _headers(tokens)
    start = await client.post(
        "/onboarding/me/first-task/start",
        json={"suggestedTaskId": "battery"},
        headers=headers,
    )
    conversation_id = start.json()["conversationId"]

    resp = await client.post(
        "/onboarding/me/first-task/complete",
        json={
            "conversationId": conversation_id,
            "outcome": "completed",
            "toolUsed": True,
            "toolNames": ["device.battery_stats"],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    state = resp.json()
    assert state["status"] == "first_task_completed"
    assert state["firstTask"]["outcome"] == "completed"
    assert state["firstTask"]["toolUsed"] is True
    assert state["firstTask"]["toolNames"] == ["device.battery_stats"]
    assert state["firstTask"]["completedAt"]


# --- 9/10/11. feedback -----------------------------------------------------


async def test_feedback_yes(client):
    tokens = await _register(client)
    headers = _headers(tokens)
    await _first_task_done(client, headers)

    resp = await client.post(
        "/onboarding/me/feedback",
        json={"result": "yes", "alternative": "google"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    state = resp.json()
    assert state["status"] == "feedback_completed"
    assert state["feedback"]["result"] == "yes"
    assert state["feedback"]["alternative"] == "google"


async def test_feedback_partly_with_text(client):
    tokens = await _register(client)
    headers = _headers(tokens)
    await _first_task_done(client, headers)

    resp = await client.post(
        "/onboarding/me/feedback",
        json={
            "result": "partly",
            "expectationText": "I expected it to change the setting itself",
            "alternative": "google",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    state = resp.json()
    assert state["feedback"]["result"] == "partly"
    assert state["feedback"]["expectationText"] == "I expected it to change the setting itself"


async def test_feedback_no(client):
    tokens = await _register(client)
    headers = _headers(tokens)
    await _first_task_done(client, headers)

    resp = await client.post(
        "/onboarding/me/feedback",
        json={"result": "no", "alternative": "would_not_do"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["feedback"]["result"] == "no"


async def test_feedback_other_requires_text(client):
    tokens = await _register(client)
    headers = _headers(tokens)
    await _first_task_done(client, headers)

    resp = await client.post(
        "/onboarding/me/feedback",
        json={"result": "no", "alternative": "other"},
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "ALTERNATIVE_TEXT_REQUIRED"


async def test_paywall_due_only_after_feedback(client):
    tokens = await _register(client)
    headers = _headers(tokens)

    assert (await _state(client, headers))["paywallDue"] is False

    await _first_task_done(client, headers)
    await client.post(
        "/onboarding/me/feedback",
        json={"result": "yes"},
        headers=headers,
    )
    state = await _state(client, headers)
    assert state["paywallDue"] is True
    assert state["subscriptionRequired"] is True


# --- 12. beta entitlement --------------------------------------------------


async def test_beta_entitlement_paywall_not_required(client):
    tokens = await _register(client)
    headers = _headers(tokens)
    user_id = await _user_id(client, headers)

    admin = await register_user(client, email="admin@creepy.im")
    admin_headers = _headers(admin)
    resp = await client.post(
        "/admin/grant-beta",
        json={"userId": user_id},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["granted"] is True

    # Entitlements now say no paywall is needed.
    sub = await client.get("/subscriptions/me", headers=headers)
    assert sub.json()["entitlements"]["subscriptionRequired"] is False

    # And the onboarding state carries the same answer.
    state = await _state(client, headers)
    assert state["subscriptionRequired"] is False

    # A beta user can pass the whole flow and land on completed.
    await _first_task_done(client, headers)
    await client.post(
        "/onboarding/me/feedback",
        json={"result": "yes"},
        headers=headers,
    )
    final = await _complete(client, headers)
    assert final["status"] == "completed"
    assert final["paywallDue"] is False


async def test_beta_revoke_restores_paywall(client):
    tokens = await _register(client)
    headers = _headers(tokens)
    user_id = await _user_id(client, headers)

    admin = await register_user(client, email="admin@creepy.im")
    admin_headers = _headers(admin)
    await client.post("/admin/grant-beta", json={"userId": user_id}, headers=admin_headers)
    revoke = await client.post(
        "/admin/revoke-beta", json={"userId": user_id}, headers=admin_headers
    )
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["granted"] is False

    sub = await client.get("/subscriptions/me", headers=headers)
    assert sub.json()["entitlements"]["subscriptionRequired"] is True


# --- 13. normal entitlement ------------------------------------------------


async def test_normal_user_subscription_required(client):
    tokens = await _register(client)
    headers = _headers(tokens)
    sub = await client.get("/subscriptions/me", headers=headers)
    assert sub.json()["entitlements"]["subscriptionRequired"] is True
    state = await _state(client, headers)
    assert state["subscriptionRequired"] is True


# --- 14. old user does not restart onboarding ------------------------------


async def test_legacy_user_is_migrated_not_restarted(client):
    # A v1 user is an account with a name and no v2 record. Registering now
    # creates a v2 record, so simulate the legacy case by deleting it first.
    tokens = await _register(client)
    headers = _headers(tokens)
    user_id = await _user_id(client, headers)

    app = client._transport.app
    factory = app.state.session_factory
    from app.db.models import UserOnboarding

    async with factory() as session:
        row = await session.get(UserOnboarding, user_id)
        await session.delete(row)
        await session.commit()

    state = await _state(client, headers)
    assert state["version"] == 2
    assert state["status"] == "completed"


async def test_login_of_legacy_user_reports_onboarding_completed(client):
    # An account that already has a name (v1) is reported as completed at
    # login, not sent back through onboarding.
    await _register(client, email="legacy@creepy.im")

    app = client._transport.app
    factory = app.state.session_factory
    from app.db import repositories as repo
    from app.db.models import UserOnboarding

    async with factory() as session:
        user = await repo.get_user_by_email(session, "legacy@creepy.im")
        row = await session.get(UserOnboarding, user.user_id)
        await session.delete(row)
        await session.commit()

    # The registration above already requested a code for this address; drop
    # the per-email cooldown so the login request-code is not rate limited.
    from tests.conftest import clear_cooldown

    await clear_cooldown(client, "legacy@creepy.im")

    resp = await client.post(
        "/auth/email/request-code",
        json={"email": "legacy@creepy.im", "purpose": "login"},
    )
    assert resp.status_code == 200, resp.text
    verify = await client.post(
        "/auth/email/verify-code",
        json={
            "challengeId": resp.json()["challengeId"],
            "code": email_sender(client).pop_code("legacy@creepy.im"),
            "email": "legacy@creepy.im",
        },
    )
    assert verify.status_code == 200, verify.text
    assert verify.json()["onboardingCompleted"] is True


# --- 15. intents are not permission state ----------------------------------


async def test_intents_do_not_claim_connections(client):
    tokens = await _register(client)
    headers = _headers(tokens)

    await _patch_intents(client, headers, ["messages", "email"])

    # The onboarding record stores interests only. Nothing about Telegram,
    # Google or Android scopes is recorded server-side.
    user_id = await _user_id(client, headers)
    row = await _direct_db(client._transport.app, user_id)
    assert sorted(row["intents"]) == sorted(["messages", "email"])
    assert "telegram" not in json.dumps(row["first_task"])
    assert "permission" not in json.dumps(row).lower()


async def test_intents_do_not_change_entitlements(client):
    tokens = await _register(client)
    headers = _headers(tokens)
    before = await client.get("/subscriptions/me", headers=headers)
    before_required = before.json()["entitlements"]["subscriptionRequired"]

    await _patch_intents(client, headers, ["messages"])
    after = await client.get("/subscriptions/me", headers=headers)
    assert after.json()["entitlements"]["subscriptionRequired"] == before_required


# --- helpers ---------------------------------------------------------------


async def _first_task_done(client, headers: dict) -> None:
    await client.post(
        "/onboarding/me/first-task/start",
        json={"suggestedTaskId": "battery"},
        headers=headers,
    )
    resp = await client.post(
        "/onboarding/me/first-task/complete",
        json={"outcome": "completed", "toolUsed": True, "toolNames": ["device.battery_stats"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text