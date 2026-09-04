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

# --- real-world entry points ---------------------------------------------


def test_a_declared_build_output_falls_back_to_source(tmp_path):
    """Every official Node MCP server declares dist/index.js and commits none of it.

    package.json points at the npm artifact, not at anything in the
    repository, so taking it literally refused all of them.
    """
    repo = tmp_path / "srv"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text(
        '{"name":"srv","bin":{"srv":"dist/index.js"},'
        '"dependencies":{"@modelcontextprotocol/sdk":"^1"}}'
    )
    (repo / "src" / "index.ts").write_text("export const ok = true;\n")

    detection = bundler.detect(repo)

    assert detection.entrypoint == "src/index.ts"
    assert any("build output" in line for line in detection.evidence)


def test_the_source_matching_the_declared_name_wins(tmp_path):
    """dist/server.js is server.ts, not whichever candidate is listed first."""
    repo = tmp_path / "srv"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text('{"name":"srv","main":"dist/server.js"}')
    (repo / "src" / "index.ts").write_text("export const wrong = true;\n")
    (repo / "src" / "server.ts").write_text("export const right = true;\n")

    assert bundler.detect(repo).entrypoint == "src/server.ts"


def test_a_root_level_entry_is_found_without_src(tmp_path):
    repo = tmp_path / "srv"
    repo.mkdir()
    (repo / "package.json").write_text('{"name":"srv","bin":{"srv":"dist/index.js"}}')
    (repo / "index.ts").write_text("export const ok = true;\n")

    assert bundler.detect(repo).entrypoint == "index.ts"


# --- what a bundle must not contain --------------------------------------


@needs_toolchain
def test_the_hashbang_is_stripped(tmp_path):
    """MCP servers are CLI binaries; the device wraps the bundle in a function.

    esbuild preserves `#!/usr/bin/env node` from the entry file, and a
    hashbang inside a function body is a syntax error — so every real server
    would have failed to evaluate on its first line.
    """
    repo = _node_repo(
        tmp_path,
        body="#!/usr/bin/env node\nglobalThis.ok = true;\n",
    )

    code = bundler.translate(repo, bundler.detect(repo), SHIMS)

    assert not code.startswith("#!")
    assert "globalThis.ok" in code


@needs_toolchain
def test_import_meta_is_replaced(tmp_path):
    """`import.meta` is module syntax and is illegal inside a function body."""
    repo = _node_repo(
        tmp_path,
        body="globalThis.here = import.meta.url;\nglobalThis.all = import.meta;\n",
    )

    code = bundler.translate(repo, bundler.detect(repo), SHIMS)

    # esbuild labels the substitution with a `// <define:import.meta>` comment,
    # so the bare string survives harmlessly. What must not survive is an
    # executable reference.
    executable = [
        line
        for line in code.splitlines()
        if "import.meta" in line and not line.lstrip().startswith("//")
    ]
    assert executable == [], executable
    assert "file:///mcp-server.js" in code


@needs_toolchain
def test_pure_node_builtins_are_shimmed_not_refused(tmp_path):
    """path and url manipulate strings; refusing over them refuses most servers.

    The official sequential-thinking server imports exactly these and nothing
    else that is unavailable.
    """
    repo = _node_repo(
        tmp_path,
        body=(
            "import path from 'node:path';\n"
            "import { fileURLToPath } from 'node:url';\n"
            "import { EventEmitter } from 'node:events';\n"
            "globalThis.joined = path.join('/a', 'b', '../c');\n"
            "globalThis.emitter = new EventEmitter();\n"
            "globalThis.here = fileURLToPath('file:///x/y.js');\n"
        ),
    )

    code = bundler.translate(repo, bundler.detect(repo), SHIMS)

    assert "node:path" not in code
    assert "node:url" not in code


@needs_toolchain
def test_the_servers_own_package_identity_is_injected(tmp_path):
    """Several servers refuse to start without reading their own package.json.

    The official sequential-thinking server calls
    `createRequire(import.meta.url)(".../package.json")` for its version and
    throws if it cannot find one. There is no file in a bundle, so the two
    fields that matter are compiled in.
    """
    repo = _node_repo(
        tmp_path,
        body=(
            "import { createRequire } from 'node:module';\n"
            "const require = createRequire(import.meta.url);\n"
            "globalThis.version = require('/anywhere/package.json').version;\n"
        ),
    )

    code = bundler.translate(repo, bundler.detect(repo), SHIMS)

    assert '"weather-mcp"' in code
    assert '"1.0.0"' in code

def test_the_npm_diagnosis_is_the_cause_not_the_trailer():
    """npm prints the reason first and housekeeping last.

    Taking the last lines — the obvious way to get "the end of the error" —
    captured the path to a debug log inside a temporary directory that had
    already been deleted, and discarded the one line that said what was wrong.
    """
    output = "\n".join(
        [
            "npm error code ETARGET",
            "npm error notarget No matching version found for @claude-flow/mcp@3.0.0-alpha.10.",
            "npm error notarget In most cases you or one of your dependencies",
            "npm error A complete log of this run can be found in:",
            "npm error     /tmp/creepy-mcp-x/repo/.npm-cache/_logs/"
            "2026-09-04T04_29_25_422Z-debug-0.log",
        ]
    )

    hints = bundler._npm_diagnosis(output)

    assert any("No matching version" in hint for hint in hints)
    assert not any("_logs/" in hint for hint in hints)
    assert not any("complete log" in hint for hint in hints)

