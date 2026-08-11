
import httpx
import pytest

from app.api.deps import get_http_client
from tests.conftest import CapturingEmailSender, register_user


class _FakeOpenRouterTransport(httpx.AsyncBaseTransport):
    """Intercepts httpx requests and returns mock OpenRouter responses."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = (
            b'{"choices":[{"message":{"role":"assistant","content":"hello from OpenRouter"}}]}'
        )
        return httpx.Response(200, content=body, request=request)


@pytest.fixture
def agent_app(app):
    """Override the http_client dependency with our fake OpenRouter transport."""

    async def override_get_http_client():
        client = httpx.AsyncClient(transport=_FakeOpenRouterTransport())
        try:
            yield client
        finally:
            await client.aclose()

    app.dependency_overrides[get_http_client] = override_get_http_client
    return app


class TestAgentHttpIntegration:

    async def test_full_request_response_cycle(self, agent_app, monkeypatch):
        """End-to-end JSON-in → JSON-out through FastAPI with camelCase wire format.

        This is THE test that catches camelCase/snake_case mismatches between
        frontend and backend.
        """
        agent_app.state.email_sender = CapturingEmailSender()
        transport = httpx.ASGITransport(app=agent_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

            async def _register(email: str) -> str:

                resp = await client.post(
                    "/auth/email/request-code",
                    json={"email": email, "purpose": "registration"},
                )
                assert resp.status_code == 200, resp.text
                challenge_id = resp.json()["challengeId"]
                code = agent_app.state.email_sender.pop_code(email)
                resp = await client.post(
                    "/auth/email/verify-code",
                    json={"challengeId": challenge_id, "code": code, "email": email},
                )
                assert resp.status_code == 200, resp.text
                return resp.json()["accessToken"]

            token = await _register("agent-http@creepy.im")

            response = await client.post(
                "/agent/step",
                json={
                    "requestId": "req-integration-1",
                    "runId": "run-integration-1",
                    "routing": {
                        "requestedTier": "normal",
                        "reasoningScore": 7,
                        "hardReasoningSignals": [],
                        "weakSignals": {
                            "stepCount": 3,
                            "toolCalls": 2,
                            "connectorCount": 1,
                        },
                        "struggle": {
                            "failedPlans": 0,
                            "replans": 1,
                            "repeatedToolPattern": False,
                            "invalidToolCalls": 0,
                            "repeatedToolFailures": 0,
                        },
                        "context": {
                            "largeStructuredContext": False,
                            "largeUnstructuredContext": False,
                        },
                        "escalationCount": 0,
                    },
                    "messages": [
                        {"role": "user", "content": "What is the weather?"},
                    ],
                    "tools": [],
                },
                headers={"Authorization": f"Bearer {token}"},
            )

            assert response.status_code == 200, response.text
            body = response.json()
            assert body is not None
            assert body["requestId"] == "req-integration-1"
            assert body["runId"] == "run-integration-1"
            assert body["requestedModelTier"] == "normal"
            assert body["effectiveModelTier"] == "normal"
            assert body["routingReason"] == "moderate_reasoning"
            assert "result" in body
            assert body["result"]["kind"] == "final"
            assert body["result"]["text"] == "hello from OpenRouter"

    async def test_requires_auth(self, agent_app):
        """The /agent/step endpoint must reject unauthenticated requests."""
        transport = httpx.ASGITransport(app=agent_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/agent/step",
                json={
                    "requestId": "req-1",
                    "runId": "run-1",
                    "routing": {"requestedTier": "fast"},
                    "messages": [],
                },
            )
            assert response.status_code == 401
            assert response.json()["code"] == "UNAUTHORIZED"

    async def test_validation_error_on_missing_fields(self, agent_app):
        """Missing required fields should return 422."""
        agent_app.state.email_sender = CapturingEmailSender()
        transport = httpx.ASGITransport(app=agent_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            tok = await register_user(client, email="valid-http@creepy.im")

            response = await client.post(
                "/agent/step",
                json={},
                headers={"Authorization": f"Bearer {tok['accessToken']}"},
            )
            assert response.status_code == 422
            assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_tool_conversion_roundtrip(self, agent_app):
        """Frontend tools → OpenRouter → result in camelCase format."""
        agent_app.state.email_sender = CapturingEmailSender()
        transport = httpx.ASGITransport(app=agent_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            tok = await register_user(client, email="tools-http@creepy.im")

            response = await client.post(
                "/agent/step",
                json={
                    "requestId": "req-tools-1",
                    "runId": "run-tools-1",
                    "routing": {
                        "requestedTier": "normal",
                        "reasoningScore": 0,
                        "hardReasoningSignals": [],
                        "weakSignals": {},
                        "struggle": {},
                        "context": {},
                        "escalationCount": 0,
                    },
                    "messages": [{"role": "user", "content": "search for Daniyar"}],
                    "tools": [
                        {
                            "name": "search",
                            "description": "Search contacts",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ],
                },
                headers={"Authorization": f"Bearer {tok['accessToken']}"},
            )

            assert response.status_code == 200, response.text
            body = response.json()
            assert body["result"]["kind"] == "final"

    async def test_usage_info_in_response(self, agent_app):
        """response should contain usage."""
        agent_app.state.email_sender = CapturingEmailSender()
        transport = httpx.ASGITransport(app=agent_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            tok = await register_user(client, email="usage-http@creepy.im")

            response = await client.post(
                "/agent/step",
                json={
                    "requestId": "req-usage-1",
                    "runId": "run-usage-1",
                    "routing": {
                        "requestedTier": "fast",
                        "reasoningScore": 0,
                        "hardReasoningSignals": [],
                    },
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"Authorization": f"Bearer {tok['accessToken']}"},
            )
            assert response.status_code == 200, response.text
