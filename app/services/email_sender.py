from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import Settings

logger = logging.getLogger("app.email")

SMTP_TIMEOUT_SECONDS = 15.0


class EmailSender:
    def __init__(self, settings: Settings) -> None:
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

        if not self._settings.brevo_smtp_username or not self._settings.brevo_smtp_password:
            if self._settings.is_production:
                raise RuntimeError("Brevo SMTP credentials are not configured")
            logger.warning(
                "dev email to=%s code=%s (Brevo SMTP credentials not set)",
                to,
                code,
            )
            return

        message = EmailMessage()
        message["From"] = self._settings.email_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        message.add_alternative(html, subtype="html")

        await asyncio.to_thread(self._send_via_smtp, message)

    def _send_via_smtp(self, message: EmailMessage) -> None:
        try:
            tls_context = ssl.create_default_context()
            with smtplib.SMTP(
                host=self._settings.brevo_smtp_host,
                port=self._settings.brevo_smtp_port,
                timeout=SMTP_TIMEOUT_SECONDS,
            ) as smtp:
                smtp.ehlo()
                smtp.starttls(context=tls_context)
                smtp.ehlo()
                smtp.login(
                    self._settings.brevo_smtp_username,
                    self._settings.brevo_smtp_password,
                )
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            logger.exception("brevo smtp failed recipient=%s", message["To"])
            raise RuntimeError("Brevo SMTP relay failed") from exc
