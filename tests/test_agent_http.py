import json

import httpx
import pytest

from app.api.deps import get_http_client
from app.llm.gateway import provider_safe_name
from tests.conftest import CapturingEmailSender, register_user


class _FakeOpenRouterTransport(httpx.AsyncBaseTransport):
    """Intercepts httpx requests and returns mock OpenRouter responses."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = (
            b'{"choices":[{"message":{"role":"assistant","content":"hello from OpenRouter"}}]}'
        )
        return httpx.Response(200, content=body, request=request)


class _MultiStepOpenRouterTransport(httpx.AsyncBaseTransport):
    """Returns tool_calls on first call, final text on subsequent calls.

    On the first call it echoes back the provider-safe alias it was sent, so
    the test verifies the full canonical -> alias -> canonical round-trip.
    """

    FINAL_RESPONSE = (
        b'{"choices":[{"message":{"role":"assistant","content":"\\u041d\\u0435 \\u043d\\u0430\\u0448\\u0451\\u043b '  # noqa: E501
        b'\\u0414\\u0430\\u043d\\u0438\\u044f\\u0440\\u0430. \\u041f\\u043e\\u043f\\u0440\\u043e\\u0431\\u0443\\u0439 '  # noqa: E501
        b"\\u0443\\u0442\\u043e\\u0447\\u043d\\u0438\\u0442\\u044c \\u0438\\u043c\\u044f.\"}}]}"
    )

    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0
        self.captured_messages: list[dict] = []
        self.captured_tools: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        body_bytes = request.read()
        request_body = json.loads(body_bytes)
        self.captured_messages = request_body.get("messages", [])
        self.captured_tools = request_body.get("tools", [])

        if self.call_count == 1:
            provider_name = self.captured_tools[0]["function"]["name"]
            tool_calls_response = json.dumps({
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_tg_1",
                            "type": "function",
                            "function": {
                                "name": provider_name,
                                "arguments": (
                                    '{"connectionId": "telegram-user-1", '
                                    + '"query": "\\u0414\\u0430\\u043d\\u0438\\u044f\\u0440"}'
                                ),
                            },
                        }],
                    },
                }],
            }).encode()
            return httpx.Response(200, content=tool_calls_response, request=request)
        return httpx.Response(200, content=self.FINAL_RESPONSE, request=request)


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


@pytest.fixture
def multi_step_agent_app(app):
    """App with a transport that simulates a tool-call → final multi-step flow."""
    transport = _MultiStepOpenRouterTransport()

    async def override_get_http_client():
        client = httpx.AsyncClient(transport=transport)
        try:
            yield client
        finally:
            await client.aclose()

    app.dependency_overrides[get_http_client] = override_get_http_client
    return app, transport


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


class TestMultiStepConversation:

    async def test_tool_call_then_result_roundtrip(self, multi_step_agent_app):
        """Full multi-step: user → tool_call → tool result → final answer.

        Verifies the frontend message format is correctly converted
        to OpenRouter format at each step.
        """
        app, transport = multi_step_agent_app
        app.state.email_sender = CapturingEmailSender()
        asgi_transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=asgi_transport, base_url="http://test") as client:
            tok = await register_user(client, email="multi-step@creepy.im")

            # --- Step 1: user asks a question → tool_call returned ---
            step1 = await client.post(
                "/agent/step",
                json={
                    "requestId": "req-ms-1",
                    "runId": "run-ms",
                    "routing": {"requestedTier": "normal", "reasoningScore": 5},
                    "messages": [{"role": "user", "content": "Найди Данияра"}],
                    "tools": [{
                        "name": "telegram.user.search_chats",
                        "description": "Search Telegram chats",
                        "inputSchema": {"type": "object", "properties": {}},
                    }],
                    "connections": [{
                        "id": "telegram-user-1",
                        "provider": "telegram",
                        "displayName": "@creepy_user",
                        "capabilities": ["search_chats", "send_message"],
                    }],
                },
                headers={"Authorization": f"Bearer {tok['accessToken']}"},
            )
            assert step1.status_code == 200, step1.text
            s1 = step1.json()
            assert s1["result"]["kind"] == "tool_calls"
            assert s1["result"]["toolCalls"] is not None
            assert s1["result"]["toolCalls"][0]["toolName"] == "telegram.user.search_chats"

            # Verify system prompt with connection info.
            assert len(transport.captured_messages) >= 2
            sys_msg = transport.captured_messages[0]
            assert sys_msg["role"] == "system"
            assert "telegram-user-1" in sys_msg["content"]
            assert "@creepy_user" in sys_msg["content"]

            # --- Step 2: frontend sends tool result → final answer ---
            step2 = await client.post(
                "/agent/step",
                json={
                    "requestId": "req-ms-2",
                    "runId": "run-ms",
                    "routing": {"requestedTier": "normal", "reasoningScore": 5},
                    "messages": [
                        {"id": "m1", "role": "user", "content": "Найди Данияра"},
                        {
                            "id": "m2",
                            "role": "assistant",
                            "content": "",
                            "toolCalls": [{
                                "id": "call_tg_1",
                                "toolName": "telegram.user.search_chats",
                                "args": {"connectionId": "telegram-user-1", "query": "Данияр"},
                            }],
                        },
                        {
                            "id": "m3",
                            "role": "tool",
                            "toolCallId": "call_tg_1",
                            "toolName": "telegram.user.search_chats",
                            "result": {"status": "success", "data": []},
                        },
                    ],
                    "tools": [{
                        "name": "telegram.user.search_chats",
                        "description": "Search Telegram chats",
                        "inputSchema": {"type": "object", "properties": {}},
                    }],
                    "connections": [{
                        "id": "telegram-user-1",
                        "provider": "telegram",
                        "displayName": "@creepy_user",
                        "capabilities": ["search_chats", "send_message"],
                    }],
                },
                headers={"Authorization": f"Bearer {tok['accessToken']}"},
            )
            assert step2.status_code == 200, step2.text
            s2 = step2.json()
            assert s2["result"]["kind"] == "final"
            assert s2["result"]["text"] is not None

            # Assistant message has tool_calls in OpenRouter format, using the
            # provider-safe alias (no dots), not the canonical MCP name.
            assistant_msgs = [m for m in transport.captured_messages if m["role"] == "assistant"]
            assert len(assistant_msgs) >= 1
            assert "tool_calls" in assistant_msgs[0]
            tc = assistant_msgs[0]["tool_calls"]
            assert len(tc) == 1
            assert tc[0]["id"] == "call_tg_1"
            assert tc[0]["type"] == "function"
            assert tc[0]["function"]["name"] == provider_safe_name("telegram.user.search_chats")
            assert "." not in tc[0]["function"]["name"]

            # Tool message has tool_call_id matching.
            tool_msgs = [m for m in transport.captured_messages if m["role"] == "tool"]
            assert len(tool_msgs) >= 1
            assert tool_msgs[0]["tool_call_id"] == "call_tg_1"
