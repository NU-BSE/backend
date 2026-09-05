from __future__ import annotations

import logging

import httpx

from app.core.config import Settings

logger = logging.getLogger("app.brevo_marketing")

BREVO_CONTACTS_URL = "https://api.brevo.com/v3/contacts"
BREVO_EVENTS_URL = "https://api.brevo.com/v3/events"
DOWNLOAD_LINK_EVENT = "app_download_link_requested"


class BrevoMarketingService:
    """Create/update a Brevo contact, put it on the invite list, then emit the automation event."""

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def request_download_invite(
        self,
        *,
        email: str,
        billing: str,
        source_path: str,
    ) -> None:
        if not self._settings.brevo_api_key:
            if self._settings.is_production:
                raise RuntimeError("BREVO_API_KEY is not configured")
            logger.warning(
                "dev download invite suppressed (BREVO_API_KEY not set) billing=%s source=%s",
                billing,
                source_path,
            )
            return

        headers = {
            "accept": "application/json",
            "api-key": self._settings.brevo_api_key,
            "content-type": "application/json",
        }

        # This call is deliberately first. The event should never enter an
        # automation before Brevo has a durable contact and list membership for
        # its recipient. updateEnabled makes retries and existing contacts safe.
        contact_response = await self._client.post(
            BREVO_CONTACTS_URL,
            headers=headers,
            json={
                "email": email,
                "listIds": [self._settings.brevo_contact_list_id],
                "updateEnabled": True,
            },
            timeout=15.0,
        )
        if contact_response.status_code >= 300:
            logger.error(
                "brevo contact upsert failed status=%s body=%s",
                contact_response.status_code,
                contact_response.text,
            )
            raise RuntimeError(f"Brevo contact upsert returned {contact_response.status_code}")

        button_name = "Start 7-day free trial" if billing == "annual" else "Start membership"
        event_response = await self._client.post(
            BREVO_EVENTS_URL,
            headers=headers,
            json={
                "event_name": DOWNLOAD_LINK_EVENT,
                "identifiers": {"email_id": email},
                "event_properties": {
                    "billing": billing,
                    "button_name": button_name,
                    "source_path": source_path,
                    "trial_days": 7 if billing == "annual" else 0,
                },
            },
            timeout=15.0,
        )
        if event_response.status_code >= 300:
            logger.error(
                "brevo custom event failed status=%s body=%s",
                event_response.status_code,
                event_response.text,
            )
            raise RuntimeError(f"Brevo event returned {event_response.status_code}")
