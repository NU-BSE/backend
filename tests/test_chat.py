import orjson

from app.services.llm import MOCK_REPLY
from app.services.usage import USAGE_PREFIX
from tests.conftest import register_user
from tests.test_admin import _user_id


async def _grant_pro(client, access_token):
    admin = await register_user(client, email="admin@creepy.im", name="Admin")
    user_id = await _user_id(client, access_token)
    resp = await client.post(
        "/admin/grant",
        json={"userId": user_id, "planCode": "pro", "days": 30},
        headers={"Authorization": f"Bearer {admin['accessToken']}"},
    )
    assert resp.status_code == 200, resp.text


def parse_ndjson(text: str) -> list[dict]:
    return [orjson.loads(line) for line in text.splitlines() if line.strip()]


async def test_chat_requires_auth(client):
    resp = await client.post("/chat/http", json={"messages": []})
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"


async def test_chat_rejects_free_plan(client):
    body = await register_user(client, email="free@creepy.im")
    resp = await client.post(
        "/chat/http",
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers={"Authorization": f"Bearer {body['accessToken']}"},
    )
    assert resp.status_code == 403
    data = resp.json()
    assert data["code"] == "CLOUD_AGENT_NOT_ALLOWED"
    assert data["planCode"] == "free"


async def test_chat_streams_agui_events_for_pro_user(client):
    body = await register_user(client, email="pro@creepy.im")
    await _grant_pro(client, body["accessToken"])

    resp = await client.post(
        "/chat/http",
        json={
            "threadId": "thread_test",
            "runId": "run_test",
            "messages": [{"role": "user", "content": "who is there?"}],
        },
        headers={"Authorization": f"Bearer {body['accessToken']}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/x-ndjson")

    events = parse_ndjson(resp.text)
    types = [event["type"] for event in events]

    assert types[0] == "RUN_STARTED"
    assert types[1] == "TEXT_MESSAGE_START"
    assert types[-2] == "TEXT_MESSAGE_END"
    assert types[-1] == "RUN_FINISHED"
    assert types.count("TEXT_MESSAGE_CONTENT") > 1
    assert "RUN_ERROR" not in types

    started, finished = events[0], events[-1]
    assert started["threadId"] == "thread_test"
    assert started["runId"] == "run_test"
    assert finished["threadId"] == "thread_test"
    assert finished["runId"] == "run_test"

    message_ids = {
        event["messageId"]
        for event in events
        if event["type"].startswith("TEXT_MESSAGE")
    }
    assert len(message_ids) == 1

    text = "".join(
        event["delta"] for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"
    )
    assert text == MOCK_REPLY


async def test_chat_usage_limit_enforced(client):
    body = await register_user(client, email="limited@creepy.im")
    await _grant_pro(client, body["accessToken"])
    user_id = await _user_id(client, body["accessToken"])

    store = client._transport.app.state.store  # type: ignore[attr-defined]
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"{USAGE_PREFIX}{user_id}:{today}"
    for _ in range(200):
        await store.incr(key, 86400)

    resp = await client.post(
        "/chat/http",
        json={"messages": [{"role": "user", "content": "one more"}]},
        headers={"Authorization": f"Bearer {body['accessToken']}"},
    )
    assert resp.status_code == 402
    data = resp.json()
    assert data["code"] == "USAGE_LIMIT_REACHED"
    assert data["retryAfterSeconds"] > 0
    assert data["limit"] == 200


async def test_chat_counts_usage(client):
    body = await register_user(client, email="counted@creepy.im")
    await _grant_pro(client, body["accessToken"])

    headers = {"Authorization": f"Bearer {body['accessToken']}"}
    payload = {"messages": [{"role": "user", "content": "hello"}]}
    first = await client.post("/chat/http", json=payload, headers=headers)
    second = await client.post("/chat/http", json=payload, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200

    me = await client.get("/subscriptions/me", headers=headers)
    assert me.status_code == 200
