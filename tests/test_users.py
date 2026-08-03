from tests.conftest import register_user


async def test_me_requires_auth(client):
    resp = await client.get("/users/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"


async def test_me_returns_profile(client):
    body = await register_user(client, email="me@creepy.im", name="Me Myself")
    resp = await client.get(
        "/users/me", headers={"Authorization": f"Bearer {body['accessToken']}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "me@creepy.im"
    assert data["name"] == "Me Myself"
    assert data["userId"]


async def test_patch_me_updates_name(client):
    body = await register_user(client, email="patch@creepy.im", name=None)
    headers = {"Authorization": f"Bearer {body['accessToken']}"}

    resp = await client.patch("/users/me", json={"name": "Patched"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Patched"

    resp = await client.get("/users/me", headers=headers)
    assert resp.json()["name"] == "Patched"


async def test_patch_me_validates(client):
    body = await register_user(client, email="patch2@creepy.im")
    headers = {"Authorization": f"Bearer {body['accessToken']}"}
    resp = await client.patch("/users/me", json={"name": ""}, headers=headers)
    assert resp.status_code == 422


async def test_me_rejects_refresh_token(client):
    body = await register_user(client, email="ref@creepy.im")
    resp = await client.get(
        "/users/me", headers={"Authorization": f"Bearer {body['refreshToken']}"}
    )
    assert resp.status_code == 401
