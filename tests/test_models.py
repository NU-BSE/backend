"""Model weight delivery.

The interesting cases are the refusals: who is told the size, who is given the
bytes, and what a filename from the network is allowed to reach.
"""

import hashlib
import json

import pytest

from app.services.model_catalog import ModelCatalog
from tests.conftest import make_settings, register_user


def _write_bundle(root, model: str, *, corrupt_size: bool = False) -> dict:
    gguf = root / model / "artifacts" / "gguf"
    gguf.mkdir(parents=True)

    weights = gguf / f"gui-owl-{model}-q4_0.gguf"
    projector = gguf / f"mmproj-gui-owl-{model}-bf16.gguf"
    weights.write_bytes(b"weights-" + model.encode())
    projector.write_bytes(b"projector-" + model.encode())

    def entry(path):
        data = path.read_bytes()
        return {
            "bytes": len(data) + (99 if corrupt_size else 0),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    manifest = {
        "files": {weights.name: entry(weights), projector.name: entry(projector)},
        "student": model,
    }
    (gguf / f"gui-owl-{model}-export-manifest.json").write_text(json.dumps(manifest))
    return manifest


@pytest.fixture
def model_settings(tmp_path):
    root = tmp_path / "models"
    for model in ("0.5b", "1b", "1.5b"):
        _write_bundle(root, model)
    settings = make_settings(tmp_path)
    settings.model_artifact_root = str(root)
    return settings


@pytest.fixture
async def model_client(model_settings):
    import httpx

    from app.main import create_app
    from tests.conftest import CapturingEmailSender

    app = create_app(model_settings)
    async with app.router.lifespan_context(app):
        app.state.email_sender = CapturingEmailSender()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _auth(client, email: str) -> dict[str, str]:
    tokens = await register_user(client, email=email)
    return {"Authorization": f"Bearer {tokens['accessToken']}"}


async def _grant_pro(client, headers) -> None:
    """Give the user a paid subscription through the admin path."""
    admin = await register_user(client, email="admin@creepy.im")
    admin_headers = {"Authorization": f"Bearer {admin['accessToken']}"}
    me = await client.get("/users/me", headers=headers)
    await client.post(
        "/admin/grant",
        json={"userId": me.json()["userId"], "planCode": "pro", "days": 30},
        headers=admin_headers,
    )


# --- catalogue -----------------------------------------------------------


def test_catalog_skips_a_profile_whose_manifest_lies(tmp_path):
    """A size that does not match the file on disk would fail every client."""
    root = tmp_path / "models"
    _write_bundle(root, "0.5b", corrupt_size=True)
    settings = make_settings(tmp_path)
    settings.model_artifact_root = str(root)

    catalog = ModelCatalog(settings)
    assert "efficient" not in catalog.profiles


def test_catalog_omits_profiles_with_no_artifacts(tmp_path):
    root = tmp_path / "models"
    _write_bundle(root, "0.5b")
    settings = make_settings(tmp_path)
    settings.model_artifact_root = str(root)

    catalog = ModelCatalog(settings)
    assert catalog.profiles == ("efficient",)


async def test_catalog_is_readable_before_paying(model_client):
    """The size is shown on the model-selection screen, which precedes the paywall."""
    headers = await _auth(model_client, "browser@creepy.im")
    resp = await model_client.get("/models/catalog", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["downloadAllowed"] is False
    profiles = {bundle["profile"] for bundle in body["bundles"]}
    assert profiles == {"efficient", "balanced", "performance"}
    for bundle in body["bundles"]:
        assert bundle["totalBytes"] > 0
        for entry in bundle["files"]:
            assert len(entry["sha256"]) == 64


async def test_catalog_requires_authentication(model_client):
    assert (await model_client.get("/models/catalog")).status_code == 401


# --- download ------------------------------------------------------------


async def test_download_refused_without_a_subscription(model_client):
    headers = await _auth(model_client, "freeloader@creepy.im")
    resp = await model_client.get(
        "/models/efficient/files/gui-owl-0.5b-q4_0.gguf", headers=headers
    )
    assert resp.status_code == 402
    assert resp.json()["code"] == "SUBSCRIPTION_REQUIRED"


async def test_download_allowed_once_subscribed(model_client):
    headers = await _auth(model_client, "payer@creepy.im")
    await _grant_pro(model_client, headers)

    catalog = await model_client.get("/models/catalog", headers=headers)
    assert catalog.json()["downloadAllowed"] is True

    resp = await model_client.get(
        "/models/efficient/files/gui-owl-0.5b-q4_0.gguf", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == b"weights-0.5b"
    # The client verifies what it got without re-reading the catalogue.
    assert resp.headers["x-model-sha256"] == hashlib.sha256(b"weights-0.5b").hexdigest()


async def test_download_supports_range_for_resume(model_client):
    headers = await _auth(model_client, "resumer@creepy.im")
    await _grant_pro(model_client, headers)

    resp = await model_client.get(
        "/models/efficient/files/gui-owl-0.5b-q4_0.gguf",
        headers={**headers, "Range": "bytes=8-"},
    )
    # A dropped mobile download must resume, not restart a gigabyte.
    assert resp.status_code == 206, resp.text
    assert resp.content == b"0.5b"


async def test_path_traversal_is_not_a_path(model_client):
    """The filename is matched against the catalogue, never joined onto a path."""
    headers = await _auth(model_client, "prober@creepy.im")
    await _grant_pro(model_client, headers)

    for name in ("../../../../etc/passwd", "..%2f..%2fetc%2fpasswd", "/etc/passwd"):
        resp = await model_client.get(
            f"/models/efficient/files/{name}", headers=headers
        )
        assert resp.status_code in (400, 404), f"{name} -> {resp.status_code}"


async def test_unknown_profile_is_not_enumerable(model_client):
    headers = await _auth(model_client, "enum@creepy.im")
    await _grant_pro(model_client, headers)

    resp = await model_client.get(
        "/models/cloud/files/gui-owl-0.5b-q4_0.gguf", headers=headers
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "MODEL_FILE_NOT_FOUND"
