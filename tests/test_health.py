async def test_root(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["checks"]["database"] is True
    assert body["checks"]["config"] is True
    assert body["checks"]["llm_configured"] is True


async def test_request_id_header(client):
    resp = await client.get("/healthz")
    assert resp.headers.get("X-Request-ID")
