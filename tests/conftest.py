import os

os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 40)

import httpx
import pytest
import pytest_asyncio

from app.core.config import Settings
from app.main import create_app

TEST_JWT_SECRET = "test-secret-" + "x" * 40


class CapturingEmailSender:
    """Stands in for Brevo in tests; records codes instead of sending them."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send_verification_code(self, *, to: str, code: str, purpose: str) -> None:
        self.sent.append({"to": to, "code": code, "purpose": purpose})

    def pop_code(self, email: str) -> str:
        for entry in reversed(self.sent):
            if entry["to"] == email:
                return entry["code"]
        raise AssertionError(f"no verification code captured for {email}")


def make_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        redis_url="",
        apply_schema_on_startup=True,
        jwt_secret=TEST_JWT_SECRET,
        admin_emails="admin@creepy.im",
        llm_mock=True,
        brevo_api_key="",
        cors_origins="http://localhost:8081",
        email_code_resend_cooldown_seconds=60,
    )


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest_asyncio.fixture
async def app(settings):
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def client(app):
    app.state.email_sender = CapturingEmailSender()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def email_sender(client) -> CapturingEmailSender:
    return client._transport.app.state.email_sender  # type: ignore[attr-defined,no-any-return]


def app_store(client):
    return client._transport.app.state.store  # type: ignore[attr-defined,no-any-return]


async def clear_cooldown(client, email: str) -> None:
    """Drop the per-email resend cooldown so tests can request codes back to back."""
    await app_store(client).delete(f"auth:cooldown:{email}")


async def request_code(client, email: str, name: str | None = None, purpose: str = "registration"):
    payload: dict[str, object] = {"email": email, "purpose": purpose}
    if name is not None:
        payload["name"] = name
    return await client.post("/auth/email/request-code", json=payload)


async def register_user(
    client, email: str = "user@creepy.im", name: str | None = "Test User"
) -> dict:
    """Drive the real request-code -> verify-code flow; returns the token body."""
    resp = await request_code(client, email=email, name=name)
    assert resp.status_code == 200, resp.text
    code = email_sender(client).pop_code(email)
    resp = await client.post(
        "/auth/email/verify-code",
        json={"challengeId": resp.json()["challengeId"], "code": code, "email": email},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()
