from __future__ import annotations

import logging

import httpx

from app.core.config import Settings

logger = logging.getLogger("app.email")

RESEND_URL = "https://api.resend.com/emails"


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
        text = (
            f"{intro}\n\n"
            f"Your verification code is: {code}\n\n"
            f"It expires in {self._settings.email_code_ttl_seconds // 60} minutes. "
            "If you did not request this, you can ignore this message."
        )
        html = (
            f"<p>{intro}</p>"
            f"<p>Your verification code is: <strong>{code}</strong></p>"
            f"<p>It expires in {self._settings.email_code_ttl_seconds // 60} minutes. "
            "If you did not request this, you can ignore this message.</p>"
        )

        if not self._settings.resend_api_key:
            if self._settings.is_production:
                raise RuntimeError("RESEND_API_KEY is not configured")
            logger.warning("dev email to=%s code=%s (RESEND_API_KEY not set)", to, code)
            return

        response = await self._client.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {self._settings.resend_api_key}"},
            json={
                "from": self._settings.email_from,
                "to": [to],
                "subject": subject,
                "text": text,
                "html": html,
            },
            timeout=15.0,
        )
        if response.status_code >= 300:
            logger.error("resend failed status=%s body=%s", response.status_code, response.text)
            raise RuntimeError(f"Resend returned {response.status_code}")
