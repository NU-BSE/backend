from tests.conftest import register_user


async def _admin_headers(client):
    body = await register_user(client, email="admin@creepy.im", name="Admin")
    return {"Authorization": f"Bearer {body['accessToken']}"}


async def test_grant_requires_auth(client):
    resp = await client.post(
        "/admin/grant", json={"userId": "x", "planCode": "pro", "days": 30}
    )
    assert resp.status_code == 401


async def test_grant_requires_admin(client):
    body = await register_user(client, email="pleb@creepy.im")
    resp = await client.post(
        "/admin/grant",
        json={"userId": "x", "planCode": "pro", "days": 30},
        headers={"Authorization": f"Bearer {body['accessToken']}"},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_grant_unknown_user(client):
    headers = await _admin_headers(client)
    resp = await client.post(
        "/admin/grant",
        json={"userId": "usr_missing", "planCode": "pro", "days": 30},
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "USER_NOT_FOUND"


async def test_grant_unknown_plan(client):
    headers = await _admin_headers(client)
    target = await register_user(client, email="target@creepy.im")
    user_id = await _user_id(client, target["accessToken"])
    resp = await client.post(
        "/admin/grant",
        json={"userId": user_id, "planCode": "platinum", "days": 30},
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "PLAN_NOT_FOUND"


async def test_grant_upgrades_user_to_pro(client):
    headers = await _admin_headers(client)
    target = await register_user(client, email="upgrade@creepy.im")
    user_id = await _user_id(client, target["accessToken"])

    resp = await client.post(
        "/admin/grant",
        json={"userId": user_id, "planCode": "pro", "days": 30},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["entitlements"]["planCode"] == "pro"
    assert data["entitlements"]["cloudAgentAllowed"] is True
    assert data["subscription"]["status"] == "active"

    # The user now sees the pro entitlements.
    me = await client.get(
        "/subscriptions/me",
        headers={"Authorization": f"Bearer {target['accessToken']}"},
    )
    assert me.json()["entitlements"]["cloudAgentAllowed"] is True


async def _user_id(client, access_token):
    resp = await client.get(
        "/users/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    return resp.json()["userId"]
