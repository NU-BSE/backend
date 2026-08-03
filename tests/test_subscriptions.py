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
