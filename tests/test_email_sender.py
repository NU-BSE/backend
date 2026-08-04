from email.message import EmailMessage

import pytest

from app.core.config import Settings
from app.services.email_sender import EmailSender

TEST_JWT_SECRET = "test-secret-" + "x" * 40


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, *, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[str] = []
        self.login_credentials: tuple[str, str] | None = None
        self.message: EmailMessage | None = None
        self.__class__.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, *, context) -> None:
        assert context is not None
        self.calls.append("starttls")

    def login(self, username: str, password: str) -> None:
        self.calls.append("login")
        self.login_credentials = (username, password)

    def send_message(self, message: EmailMessage) -> None:
        self.calls.append("send_message")
        self.message = message


@pytest.mark.asyncio
async def test_send_verification_code_uses_brevo_starttls(monkeypatch) -> None:
    FakeSMTP.instances.clear()
    monkeypatch.setattr("app.services.email_sender.smtplib.SMTP", FakeSMTP)

    settings = Settings(
        _env_file=None,
        app_env="test",
        jwt_secret=TEST_JWT_SECRET,
        llm_mock=True,
        brevo_smtp_username="smtp-user@example.com",
        brevo_smtp_password="smtp-key",
        email_from="Creepy.IM <verified-sender@example.com>",
    )
    sender = EmailSender(settings)

    await sender.send_verification_code(
        to="user@example.com",
        code="123456",
        purpose="registration",
    )

    smtp = FakeSMTP.instances[0]
    assert smtp.host == "smtp-relay.brevo.com"
    assert smtp.port == 587
    assert smtp.calls == ["ehlo", "starttls", "ehlo", "login", "send_message"]
    assert smtp.login_credentials == ("smtp-user@example.com", "smtp-key")
    assert smtp.message is not None
    from_header = smtp.message["From"]
    assert from_header.addresses[0].display_name == "Creepy.IM"
    assert from_header.addresses[0].addr_spec == "verified-sender@example.com"
    assert smtp.message["To"] == "user@example.com"
    assert smtp.message["Subject"] == "Your Creepy.IM sign-in code"
    assert "123456" in smtp.message.get_body(preferencelist=("plain",)).get_content()
    assert "<strong>123456</strong>" in smtp.message.get_body(
        preferencelist=("html",)
    ).get_content()
