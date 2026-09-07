import json

import httpx
import pytest

from app.services.brevo_marketing import BrevoMarketingService


@pytest.mark.asyncio
async def test_brevo_marketing_upserts_contact_before_event(settings):
    settings.brevo_api_key = "xkeysib-test"
    settings.brevo_contact_list_id = 2
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen.append((str(request.url), body))
        if request.url.path == "/v3/contacts":
            return httpx.Response(201, json={"id": 123})
        if request.url.path == "/v3/events":
            return httpx.Response(204)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = BrevoMarketingService(client, settings)
        await service.request_download_invite(
            email="person@example.com",
            billing="annual",
            source_path="/guides/android-phone-getting-hot-battery-drain",
        )

    assert [url for url, _ in seen] == [
        "https://api.brevo.com/v3/contacts",
        "https://api.brevo.com/v3/events",
    ]
    assert seen[0][1] == {
        "email": "person@example.com",
        "listIds": [2],
        "updateEnabled": True,
    }
    assert seen[1][1]["event_name"] == "app_download_link_requested"
    assert seen[1][1]["identifiers"] == {"email_id": "person@example.com"}
    assert seen[1][1]["event_properties"] == {
        "billing": "annual",
        "button_name": "Start 7-day free trial",
        "source_path": "/guides/android-phone-getting-hot-battery-drain",
        "trial_days": 7,
    }
