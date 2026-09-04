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
    _write_bundle(root, "2b")
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
    _write_bundle(root, "2b", corrupt_size=True)
    settings = make_settings(tmp_path)
    settings.model_artifact_root = str(root)

    catalog = ModelCatalog(settings)
    assert "on-device" not in catalog.profiles


def test_catalog_omits_profiles_with_no_artifacts(tmp_path):
    """Nothing on disk is advertised as nothing, not as a download that 404s."""
    root = tmp_path / "models"
    root.mkdir(parents=True)
    settings = make_settings(tmp_path)
    settings.model_artifact_root = str(root)

    assert ModelCatalog(settings).profiles == ()

    # And the same root, once the bundle is exported, offers exactly the one
    # profile that has artifacts.
    _write_bundle(root, "2b")
    assert ModelCatalog(settings).profiles == ("on-device",)


async def test_catalog_is_readable_before_paying(model_client):
    """The size is shown on the model-selection screen, which precedes the paywall."""
    headers = await _auth(model_client, "browser@creepy.im")
    resp = await model_client.get("/models/catalog", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["downloadAllowed"] is False
    profiles = {bundle["profile"] for bundle in body["bundles"]}
    assert profiles == {"on-device"}
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
        "/models/on-device/files/gui-owl-2b-q4_0.gguf", headers=headers
    )
    assert resp.status_code == 402
    assert resp.json()["code"] == "SUBSCRIPTION_REQUIRED"


async def test_download_allowed_once_subscribed(model_client):
    headers = await _auth(model_client, "payer@creepy.im")
    await _grant_pro(model_client, headers)

    catalog = await model_client.get("/models/catalog", headers=headers)
    assert catalog.json()["downloadAllowed"] is True

    resp = await model_client.get(
        "/models/on-device/files/gui-owl-2b-q4_0.gguf", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == b"weights-2b"
    # The client verifies what it got without re-reading the catalogue.
    assert resp.headers["x-model-sha256"] == hashlib.sha256(b"weights-2b").hexdigest()


async def test_download_supports_range_for_resume(model_client):
    headers = await _auth(model_client, "resumer@creepy.im")
    await _grant_pro(model_client, headers)

    resp = await model_client.get(
        "/models/on-device/files/gui-owl-2b-q4_0.gguf",
        headers={**headers, "Range": "bytes=8-"},
    )
    # A dropped mobile download must resume, not restart a gigabyte.
    assert resp.status_code == 206, resp.text
    assert resp.content == b"2b"


async def test_path_traversal_is_not_a_path(model_client):
    """The filename is matched against the catalogue, never joined onto a path."""
    headers = await _auth(model_client, "prober@creepy.im")
    await _grant_pro(model_client, headers)

    for name in ("../../../../etc/passwd", "..%2f..%2fetc%2fpasswd", "/etc/passwd"):
        resp = await model_client.get(
            f"/models/on-device/files/{name}", headers=headers
        )
        assert resp.status_code in (400, 404), f"{name} -> {resp.status_code}"


async def test_unknown_profile_is_not_enumerable(model_client):
    headers = await _auth(model_client, "enum@creepy.im")
    await _grant_pro(model_client, headers)

    resp = await model_client.get(
        "/models/cloud/files/gui-owl-2b-q4_0.gguf", headers=headers
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "MODEL_FILE_NOT_FOUND"

def test_the_real_2b_filenames_are_published(tmp_path):
    """The converter's actual output, not the naming this predates.

    `convert.sh` emits `gui-owl-2b-q4_k_m.gguf` and
    `gui-owl-2b-mmproj-f16.gguf`. The old suffix allow-list matched neither, so
    the profile was published with zero files — which reads, from the app, as a
    model that does not exist. The un-quantized text intermediate must still be
    excluded, and the projector must be labelled as one even though its name
    does not begin with "mmproj".
    """
    root = tmp_path / "models"
    gguf = root / "2b" / "artifacts" / "gguf"
    gguf.mkdir(parents=True)

    weights = gguf / "gui-owl-2b-q4_k_m.gguf"
    projector = gguf / "gui-owl-2b-mmproj-f16.gguf"
    intermediate = gguf / "gui-owl-2b-f16.gguf"
    weights.write_bytes(b"weights")
    projector.write_bytes(b"projector")
    intermediate.write_bytes(b"intermediate-that-must-not-ship")

    def entry(path):
        data = path.read_bytes()
        return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    (gguf / "gui-owl-2b-export-manifest.json").write_text(
        json.dumps(
            {
                "files": {
                    weights.name: entry(weights),
                    projector.name: entry(projector),
                    intermediate.name: entry(intermediate),
                },
                "student": "2b",
            }
        )
    )

    settings = make_settings(tmp_path)
    settings.model_artifact_root = str(root)
    catalog = ModelCatalog(settings)

    assert catalog.profiles == ("on-device",)
    bundle = catalog.bundle("on-device")
    names = {item.name: item.role for item in bundle.files}
    assert names == {
        "gui-owl-2b-q4_k_m.gguf": "weights",
        "gui-owl-2b-mmproj-f16.gguf": "projector",
    }

