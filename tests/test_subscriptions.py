from tests.conftest import register_user


async def test_plans_public_catalog(client):
    resp = await client.get("/subscriptions/plans")
    assert resp.status_code == 200
    plans = resp.json()["plans"]
    codes = {plan["code"] for plan in plans}
    assert {"free", "pro"} <= codes

    pro = next(plan for plan in plans if plan["code"] == "pro")
    assert pro["agentAccess"] is True
    assert pro["cloudAgentAllowed"] is True
    assert pro["maxAgentMessagesPerDay"] == 200

    free = next(plan for plan in plans if plan["code"] == "free")
    assert free["agentAccess"] is True
    assert free["cloudAgentAllowed"] is False


async def test_me_requires_auth(client):
    resp = await client.get("/subscriptions/me")
    assert resp.status_code == 401


async def test_new_user_gets_free_plan_with_entitlements(client):
    body = await register_user(client, email="sub@creepy.im")
    headers = {"Authorization": f"Bearer {body['accessToken']}"}

    resp = await client.get("/subscriptions/me", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["subscription"] is not None
    assert data["subscription"]["status"] == "active"

    entitlements = data["entitlements"]
    assert entitlements["planCode"] == "free"
    assert entitlements["agentAccess"] is True
    assert entitlements["cloudAgentAllowed"] is False


async def test_only_the_annual_plan_carries_a_trial(client):
    """The Play offer (trial-2) hangs off the annual base plan only.

    A trial advertised on monthly is one the store will never grant, and the
    user finds out at the moment they are charged.
    """
    plans = (await client.get("/subscriptions/plans")).json()["plans"]
    by_code = {plan["code"]: plan for plan in plans}

    assert by_code["pro"]["trialDays"] == 0
    assert by_code["pro_annual"]["trialDays"] == 7
    assert by_code["free"]["trialDays"] == 0


def test_base_plan_ids_are_the_console_ids():
    """These must be copied from the Play Console, never invented.

    Readable ids were made up on both sides once, and Play answered "no active
    offer for creepyim-pro-annual" for a plan that was published and active —
    the console had generated plan-2. This pins the mapping so a rename here
    is a deliberate act rather than a guess.
    """
    from app.db.schema import (
        PLAY_BASE_PLAN_ANNUAL,
        PLAY_BASE_PLAN_MONTHLY,
        PLAY_BASE_PLAN_TO_CODE,
    )

    assert PLAY_BASE_PLAN_MONTHLY == "plan-1"
    assert PLAY_BASE_PLAN_ANNUAL == "plan-2"
    assert PLAY_BASE_PLAN_TO_CODE == {"plan-1": "pro", "plan-2": "pro_annual"}
