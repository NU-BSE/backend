"""Translating MCP servers for the device.

The interesting cases are the refusals. A Node server that can be translated is
the easy half; what matters is that a Python server, a repository that is not a
server at all, and a server reaching for parts of Node a phone does not have
each fail in a way that names the actual reason.
"""

import os
import shutil
from pathlib import Path

import pytest

from app.services import mcp_bundler as bundler
from tests.conftest import register_user

SHIMS = Path("app/services/mcp_host")


def _node_repo(root: Path, *, entry: str = "src/index.mjs", body: str | None = None) -> Path:
    repo = root / "weather-mcp"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text(
        '{"name":"weather-mcp","version":"1.0.0",'
        f'"bin":{{"weather-mcp":"{entry}"}},'
        '"dependencies":{"@modelcontextprotocol/server":"^2.0.0"}}'
    )
    (repo / ".env.example").write_text("# key\nOPENWEATHER_API_KEY=\nUNITS=metric\n")
    (repo / "README.md").write_text("Set OPENWEATHER_API_KEY. LOG_LEVEL is optional.\n")
    (repo / entry).write_text(body if body is not None else "export const ok = true;\n")
    return repo


# --- detection -----------------------------------------------------------


def test_detects_a_node_server(tmp_path):
    detection = bundler.detect(_node_repo(tmp_path))

    assert detection.runtime == "NODE"
    assert detection.entrypoint == "src/index.mjs"
    # The SDK in the dependency list is what separates "an MCP server" from
    # "some Node project", so it must move the confidence.
    assert detection.confidence > 0.9
    assert any("bin.weather-mcp" in line for line in detection.evidence)


def test_dotenv_keys_are_required_and_readme_keys_are_not(tmp_path):
    """A README is prose; a name found only there is a guess, not a demand."""
    detection = bundler.detect(_node_repo(tmp_path))
    variables = {item.name: item.required for item in detection.required_environment}

    assert variables["OPENWEATHER_API_KEY"] is True
    # A single-word dotenv key is still configuration. Requiring an underscore
    # everywhere silently dropped exactly these.
    assert variables["UNITS"] is True
    assert variables["LOG_LEVEL"] is False


def test_python_servers_are_refused_with_the_reason(tmp_path):
    repo = tmp_path / "notes-mcp"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'notes-mcp'\n")

    with pytest.raises(bundler.BundleError) as caught:
        bundler.detect(repo)

    assert caught.value.type == bundler.BundleErrorType.RUNTIME_NOT_SUPPORTED
    assert "Python" in str(caught.value)
    assert any("pyproject.toml" in hint for hint in caught.value.hints)


def test_a_repository_that_is_not_a_server_says_so(tmp_path):
    repo = tmp_path / "notes"
    repo.mkdir()
    (repo / "README.md").write_text("just some notes\n")

    with pytest.raises(bundler.BundleError) as caught:
        bundler.detect(repo)

    assert caught.value.type == bundler.BundleErrorType.NO_DETECTOR_MATCHED


def test_a_node_project_with_no_entrypoint_is_refused(tmp_path):
    repo = tmp_path / "empty-mcp"
    repo.mkdir()
    (repo / "package.json").write_text('{"name":"empty-mcp"}')

    with pytest.raises(bundler.BundleError) as caught:
        bundler.detect(repo)

    assert caught.value.type == bundler.BundleErrorType.ENTRYPOINT_NOT_FOUND


# --- fetching ------------------------------------------------------------


@pytest.mark.parametrize("url", ["git@github.com:example/mcp.git", "ssh://git@host/mcp.git"])
def test_ssh_urls_are_refused(tmp_path, url):
    """An ssh URL would clone using the *server's* keys, not the caller's."""
    with pytest.raises(bundler.BundleError) as caught:
        bundler.clone(url, None, tmp_path / "repo")

    assert caught.value.type == bundler.BundleErrorType.UNSUPPORTED_REPOSITORY


# --- translation ---------------------------------------------------------


def _toolchain_available() -> bool:
    has_esbuild = bool(os.environ.get("ESBUILD_BIN")) or shutil.which("esbuild") is not None
    return bool(shutil.which("npm")) and has_esbuild


needs_toolchain = pytest.mark.skipif(
    not _toolchain_available(),
    reason="npm and esbuild are required to translate a server",
)


@needs_toolchain
def test_a_server_reaching_for_the_filesystem_is_refused_by_name(tmp_path):
    """The refusal has to name the module, or it points the user nowhere."""
    repo = _node_repo(
        tmp_path,
        body="import { readFileSync } from 'node:fs';\nexport const x = readFileSync;\n",
    )

    detection = bundler.detect(repo)
    with pytest.raises(bundler.BundleError) as caught:
        bundler.translate(repo, detection, SHIMS)

    assert caught.value.type == bundler.BundleErrorType.RUNTIME_NOT_SUPPORTED
    assert "fs" in str(caught.value)
    assert any("cannot do inside the app sandbox" in hint for hint in caught.value.hints)


@needs_toolchain
def test_translation_produces_a_bundle_with_no_module_syntax(tmp_path):
    """The device wraps the bundle in an async function to allow top-level await.

    That only works if esbuild fully inlined every dependency: a surviving
    `import` or `export` statement is a syntax error inside a function body, and
    it would fail on the phone rather than here.
    """
    repo = _node_repo(tmp_path, body="globalThis.__ok = await Promise.resolve(1);\n")

    detection = bundler.detect(repo)
    code = bundler.translate(repo, detection, SHIMS)

    for line in code.splitlines():
        assert not line.startswith(("import ", "export ")), line

    # And the top-level await the server was written with survived.
    assert "await" in code


@needs_toolchain
def test_the_stdio_transport_is_replaced_by_the_host_bridge(tmp_path):
    """A translated server must talk to the app, not to file descriptors."""
    repo = _node_repo(
        tmp_path,
        body=(
            "import { StdioServerTransport } from '@modelcontextprotocol/server/stdio';\n"
            "globalThis.__transport = new StdioServerTransport();\n"
        ),
    )
    # The alias resolves without the package installed, which is the point:
    # nothing of the real stdio transport reaches the bundle.
    detection = bundler.detect(repo)
    code = bundler.translate(repo, detection, SHIMS)

    assert "__creepyMcpHost" in code
    assert "node:process" not in code


# --- the endpoint --------------------------------------------------------


async def test_translation_requires_authentication(client):
    response = await client.post("/mcp/translate", json={"url": "https://example.com/x"})
    assert response.status_code == 401


async def test_a_bundle_id_is_never_a_path(client):
    """The id becomes a path segment, and `..` is also a string."""
    token = (await register_user(client, "mcp@creepy.im"))["accessToken"]
    headers = {"Authorization": f"Bearer {token}"}

    for candidate in ("../../etc/passwd", "..", "not-hex", "a" * 100):
        response = await client.get(f"/mcp/bundles/{candidate}", headers=headers)
        assert response.status_code in (400, 404), f"{candidate} -> {response.status_code}"


async def test_an_unfetchable_repository_reports_the_reason(client, monkeypatch):
    token = (await register_user(client, "fetch@creepy.im"))["accessToken"]
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.post(
        "/mcp/translate",
        json={"url": "git@github.com:example/private.git"},
        headers=headers,
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == bundler.BundleErrorType.UNSUPPORTED_REPOSITORY
    # The hint is the actionable half; losing it leaves only "unsupported".
    assert body["hints"]
