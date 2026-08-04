import json

import httpx
import pytest

from app.core.config import Settings
from app.services.email_sender import BREVO_SEND_EMAIL_URL, EmailSender

TEST_JWT_SECRET = "test-secret-" + "x" * 40


@pytest.mark.asyncio
async def test_send_verification_code_uses_brevo_api_key() -> None:
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        assert request.method == "POST"
        assert str(request.url) == BREVO_SEND_EMAIL_URL
        assert request.headers["api-key"] == "brevo-api-key"
        assert request.headers["accept"] == "application/json"

        payload = json.loads(request.content)
        assert payload["sender"] == {
            "name": "Creepy.IM",
            "email": "verified-sender@example.com",
        }
        assert payload["to"] == [{"email": "user@example.com"}]
        assert payload["subject"] == "Your Creepy.IM sign-in code"
        assert "<strong>123456</strong>" in payload["htmlContent"]
        return httpx.Response(201, json={"messageId": "message-id"})

    settings = Settings(
        _env_file=None,
        app_env="test",
        jwt_secret=TEST_JWT_SECRET,
        llm_mock=True,
        brevo_api_key="brevo-api-key",
        email_from="Creepy.IM <verified-sender@example.com>",
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        sender = EmailSender(client, settings)
        await sender.send_verification_code(
            to="user@example.com",
            code="123456",
            purpose="registration",
        )

    assert called
