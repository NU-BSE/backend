from __future__ import annotations

import logging
from email.utils import parseaddr

import httpx

from app.core.config import Settings

logger = logging.getLogger("app.email")

BREVO_SEND_EMAIL_URL = "https://api.brevo.com/v3/smtp/email"


class EmailSender:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def send_verification_code(self, *, to: str, code: str, purpose: str) -> None:
        subject = "Your Creepy.IM sign-in code"
        intro = (
            "Finish creating your Creepy.IM account"
            if purpose == "registration"
            else "Sign in to Creepy.IM"
        )
        html = (
            f"<p>{intro}</p>"
            f"<p>Your verification code is: <strong>{code}</strong></p>"
            f"<p>It expires in {self._settings.email_code_ttl_seconds // 60} minutes. "
            "If you did not request this, you can ignore this message.</p>"
        )

        if not self._settings.brevo_api_key:
            if self._settings.is_production:
                raise RuntimeError("BREVO_API_KEY is not configured")
            logger.warning("dev email to=%s code=%s (BREVO_API_KEY not set)", to, code)
            return

        sender_name, sender_email = parseaddr(self._settings.email_from)
        if not sender_email:
            raise RuntimeError("EMAIL_FROM must contain a valid email address")

        response = await self._client.post(
            BREVO_SEND_EMAIL_URL,
            headers={
                "accept": "application/json",
                "api-key": self._settings.brevo_api_key,
                "content-type": "application/json",
            },
            json={
                "sender": {
                    "name": sender_name or sender_email,
                    "email": sender_email,
                },
                "to": [{"email": to}],
                "subject": subject,
                "htmlContent": html,
            },
            timeout=15.0,
        )
        if response.status_code >= 300:
            logger.error(
                "brevo api failed status=%s body=%s",
                response.status_code,
                response.text,
            )
            raise RuntimeError(f"Brevo returned {response.status_code}")
