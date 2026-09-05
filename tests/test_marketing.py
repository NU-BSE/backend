import pytest


class FakeBrevoMarketingService:
    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []

    async def request_download_invite(
        self,
        *,
        email: str,
        billing: str,
        source_path: str,
    ) -> None:
        self.requests.append(
            {
                "email": email,
                "billing": billing,
                "source_path": source_path,
            }
        )


@pytest.mark.asyncio
async def test_download_invite_creates_contact_event_once(client):
    fake = FakeBrevoMarketingService()
    client._transport.app.state.brevo_marketing_service = fake  # type: ignore[attr-defined]

    payload = {
        "email": "New.User@Example.com",
        "billing": "annual",
        "sourcePath": "/guides/android-phone-getting-hot-battery-drain",
    }
    response = await client.post("/marketing/download-invite", json=payload)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "alreadyRequested": False}
    assert fake.requests == [
        {
            "email": "new.user@example.com",
            "billing": "annual",
            "source_path": "/guides/android-phone-getting-hot-battery-drain",
        }
    ]

    duplicate = await client.post("/marketing/download-invite", json=payload)
    assert duplicate.status_code == 200
    assert duplicate.json() == {"ok": True, "alreadyRequested": True}
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_download_invite_validates_input(client):
    response = await client.post(
        "/marketing/download-invite",
        json={"email": "not-an-email", "billing": "weekly", "sourcePath": "/"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
