"""Turn an MCP server repository into something an Android phone can run.

The problem this exists to solve: MCP servers are written to be *processes*.
They are spawned, they read stdin and write stdout, and they import their
language's standard library. A phone can do none of that — Android gives an app
no way to exec an interpreter it did not ship, and the app's runtime is Hermes,
not Node.

So the code is translated here instead, on a machine that does have git, npm
and a filesystem, into a single self-contained JavaScript bundle that the app
evaluates in-process. Nothing is spawned on the device and nothing is proxied
off it: the server ends up running locally, which is the whole point.

Three translations make that work, and each replaces something the phone lacks:

* **The stdio transport becomes a host bridge.** The bundler aliases the SDK's
  stdio module to `mcp_host/stdio-shim.mjs`, so a server that ends with
  `server.connect(new StdioServerTransport())` connects to the app instead of
  to file descriptors. The server's own source is never edited.
* **`process` becomes a shim.** `process.env` is how almost every MCP server
  takes its configuration; refusing servers that read it would refuse most of
  them. The host populates it from the values the user supplied.
* **Everything else Node-only is refused, loudly.** A server that needs
  `node:fs`, `node:child_process` or a native addon cannot work on a phone, and
  a bundle that pretends otherwise would fail at the user's first tool call
  with an error pointing nowhere. esbuild's unresolved-import errors are parsed
  back into a list of the builtins that caused it, so the refusal names them.

Python and JVM servers are rejected at detection. There is no honest way to run
CPython or load a jar inside an Android app that Google Play would accept, and
the alternative — shipping an interpreter per server — is not a translation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("app.mcp.bundler")


class BundleErrorType:
    """Vocabulary shared with the app, so a failure renders as itself."""

    UNSUPPORTED_REPOSITORY = "UnsupportedRepository"
    REPOSITORY_FETCH_FAILED = "RepositoryFetchFailed"
    NO_DETECTOR_MATCHED = "NoDetectorMatched"
    RUNTIME_NOT_SUPPORTED = "RuntimeNotSupported"
    ENTRYPOINT_NOT_FOUND = "EntrypointNotFound"
    PREPARATION_FAILED = "PreparationFailed"
    TRANSLATION_FAILED = "TranslationFailed"
    TOOLCHAIN_UNAVAILABLE = "ToolchainUnavailable"


class BundleError(Exception):
    def __init__(self, type_: str, message: str, hints: list[str] | None = None) -> None:
        super().__init__(message)
        self.type = type_
        self.hints = hints or []


# A shallow clone of a server repository. Anything larger is not an MCP server
# with a few hundred lines of glue; it is a mistake, and downloading it would
# be the expensive way to find that out.
MAX_REPOSITORY_BYTES = 256 * 1024 * 1024
CLONE_TIMEOUT_SECONDS = 120
INSTALL_TIMEOUT_SECONDS = 300
BUNDLE_TIMEOUT_SECONDS = 120

# Node builtins that are pure computation, shimmed rather than refused.
#
# Refusing a server for importing `node:path` would be refusing it for string
# manipulation. None of these touches the host: they are data structures,
# formatters and calling-convention adapters, and every one of them is common
# enough that excluding it would exclude most real servers. The official
# sequential-thinking server needs three of them and nothing else.
SHIMMED_BUILTINS = (
    "path",
    "url",
    "events",
    "util",
    "module",
    "fs",
    "os",
    "http",
    "https",
)

# Modules whose absence is fatal on a device. Each maps to the reason, because
# "cannot resolve node:fs" tells a user nothing about what to do next.
UNSUPPORTED_BUILTINS = {
    "child_process": "spawns other programs, which Android does not permit",
    "worker_threads": "starts OS threads the JavaScript runtime does not have",
    "net": "opens raw sockets",
    "tls": "opens raw TLS sockets",
    "dgram": "opens UDP sockets",
    "cluster": "forks worker processes",
    "v8": "reaches into the V8 engine, and the app runs Hermes",
    "vm": "compiles code at runtime",
}

# Import specifiers for the stdio transport across SDK generations. Both are
# aliased: a repository pinned to either package must translate the same way.
STDIO_SPECIFIERS = (
    "@modelcontextprotocol/sdk/server/stdio.js",
    "@modelcontextprotocol/sdk/server/stdio",
    "@modelcontextprotocol/server/stdio",
)

# Prose: require at least one underscore, or every capitalised word in a README
# becomes a "required" variable.
_ENV_NAME_RE = re.compile(r"\b([A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)+)\b")
# A dotenv key needs no such caution.
_DOTENV_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*")

# Names that look like environment variables in prose but are not configuration.
_ENV_NAME_DENYLIST = {
    "MCP_SERVER",
    "NODE_ENV",
    "NPM_CONFIG",
    "JSON_RPC",
    "README_MD",
    "MIT_LICENSE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
}


@dataclass(frozen=True)
class EnvironmentVariable:
    name: str
    required: bool
    description: str | None = None


@dataclass(frozen=True)
class Detection:
    runtime: str
    entrypoint: str | None
    evidence: list[str] = field(default_factory=list)
    required_environment: list[EnvironmentVariable] = field(default_factory=list)
    confidence: float = 0.0


@dataclass(frozen=True)
class Bundle:
    """A translated server, ready to be evaluated on a device."""

    bundle_id: str
    code: str
    sha256: str
    bytes: int
    detection: Detection


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a build step with no inherited environment beyond what is passed.

    The repository is untrusted, so nothing from this process's environment —
    which holds the licence signing key and the database URL — is allowed to
    reach a build step that might execute repository code.
    """
    base = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(cwd or "/tmp"),
        # npm writes here; without it npm falls back to $HOME and can escape
        # the temporary directory this build is supposed to be confined to.
        "npm_config_cache": str((cwd or Path("/tmp")) / ".npm-cache"),
    }
    # args is always a list built here, never a shell string, so there is no
    # shell to inject into.
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**base, **(env or {})},
        check=False,
    )


def clone(url: str, ref: str | None, destination: Path) -> None:
    """Shallow-clone a repository.

    `--depth 1` because history is irrelevant to what the server *is*, and a
    deep clone of a large repository is the slowest possible way to reach the
    same package.json.
    """
    if not url.startswith(("https://", "http://")):
        # ssh:// and git@ would use the server's own deploy keys, letting a
        # caller read any private repository the host can reach.
        raise BundleError(
            BundleErrorType.UNSUPPORTED_REPOSITORY,
            "Only http(s) repository URLs are supported.",
            ["An ssh:// or git@ URL would use this server's credentials, not yours."],
        )

    args = ["git", "clone", "--depth", "1", "--single-branch", "--no-tags"]
    if ref:
        args += ["--branch", ref]
    args += ["--", url, str(destination)]

    try:
        result = _run(args, timeout=CLONE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise BundleError(
            BundleErrorType.REPOSITORY_FETCH_FAILED,
            "The repository took too long to download.",
        ) from exc
    except FileNotFoundError as exc:
        raise BundleError(
            BundleErrorType.TOOLCHAIN_UNAVAILABLE,
            "git is not installed on the server.",
        ) from exc

    if result.returncode != 0:
        # git's stderr names the real cause — a bad ref, a private repository,
        # a typo'd host — and paraphrasing it would only lose that.
        detail = (result.stderr or "").strip().splitlines()
        raise BundleError(
            BundleErrorType.REPOSITORY_FETCH_FAILED,
            detail[-1] if detail else "The repository could not be downloaded.",
            [f"Checked out ref: {ref}"] if ref else [],
        )

    size = sum(f.stat().st_size for f in destination.rglob("*") if f.is_file())
    if size > MAX_REPOSITORY_BYTES:
        raise BundleError(
            BundleErrorType.UNSUPPORTED_REPOSITORY,
            f"The repository is {size // (1024 * 1024)} MB, larger than the "
            f"{MAX_REPOSITORY_BYTES // (1024 * 1024)} MB limit.",
        )


# Where a source entry lives when package.json points at a build output, in the
# order a project is most likely to use.
_ENTRY_CANDIDATES = (
    "src/index.ts",
    "src/index.mts",
    "src/index.js",
    "src/index.mjs",
    "src/main.ts",
    "src/server.ts",
    "src/cli.ts",
    "index.ts",
    "index.mts",
    "index.js",
    "index.mjs",
    "main.ts",
    "server.ts",
)

_SOURCE_EXTENSIONS = (".ts", ".mts", ".tsx", ".js", ".mjs", ".jsx")


def _source_entry(repo: Path, declared: str | None) -> str | None:
    """Find the source a declared build output was built from.

    `dist/index.js` is tried as `src/index.ts` and its siblings first, because
    that mapping is nearly universal and gets the *right* file rather than
    merely a plausible one — a repository with several entry points would
    otherwise be bundled from whichever one happens to be listed first below.
    """
    if declared:
        stem = Path(declared).with_suffix("")
        parts = stem.parts
        # Strip a leading build directory: dist/index -> index, build/x/y -> x/y.
        if parts and parts[0] in {"dist", "build", "out", "lib"}:
            stem = Path(*parts[1:]) if len(parts) > 1 else Path(stem.name)
        for prefix in ("src", ""):
            for extension in _SOURCE_EXTENSIONS:
                candidate = (Path(prefix) / stem).with_suffix(extension)
                if (repo / candidate).is_file():
                    return candidate.as_posix()

    for name in _ENTRY_CANDIDATES:
        if (repo / name).is_file():
            return name
    return None


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _scan_environment(repo: Path) -> list[EnvironmentVariable]:
    """Which variables the server expects.

    Two sources, in order of how much they can be trusted. `.env.example` is
    written to be read by a machine and every name in it is configuration.
    A README is prose, so a name found only there is reported as optional —
    telling a user something is required when it is not blocks them, and this
    heuristic is not good enough to earn that.
    """
    found: dict[str, EnvironmentVariable] = {}

    for name in ("env.example", ".env.example", ".env.sample", ".env.template"):
        example = repo / name
        if not example.is_file():
            continue
        for line in example.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip().removeprefix("export ").strip()
            # A looser pattern than the README scan uses on purpose. In a
            # dotenv file the key position is unambiguous, so a single-word
            # name like UNITS or PORT is certainly configuration; requiring an
            # underscore here silently dropped exactly those.
            if _DOTENV_KEY_RE.fullmatch(key) and key not in _ENV_NAME_DENYLIST:
                found[key] = EnvironmentVariable(name=key, required=True)

    for name in ("README.md", "readme.md", "README.MD", "README"):
        readme = repo / name
        if not readme.is_file():
            continue
        text = readme.read_text(encoding="utf-8", errors="replace")
        for match in _ENV_NAME_RE.finditer(text):
            key = match.group(1)
            if key in found or key in _ENV_NAME_DENYLIST:
                continue
            found[key] = EnvironmentVariable(
                name=key,
                required=False,
                description="Mentioned in the README; may not be required.",
            )
        break

    return sorted(found.values(), key=lambda item: (not item.required, item.name))


def detect(repo: Path) -> Detection:
    """Work out what this repository is, and whether it can be translated."""
    package_json = _read_json(repo / "package.json")

    if package_json is not None:
        evidence = ["package.json"]
        entry: str | None = None

        bin_field = package_json.get("bin")
        if isinstance(bin_field, str):
            entry = bin_field
            evidence.append(f"package.json: bin = {bin_field}")
        elif isinstance(bin_field, dict) and bin_field:
            first_key = sorted(bin_field)[0]
            value = bin_field[first_key]
            if isinstance(value, str):
                entry = value
                evidence.append(f"package.json: bin.{first_key} = {value}")

        if entry is None:
            for key in ("module", "main"):
                value = package_json.get(key)
                if isinstance(value, str):
                    entry = value
                    evidence.append(f"package.json: {key} = {value}")
                    break

        # What package.json declares is usually a *build output*. A
        # TypeScript MCP server publishes dist/index.js to npm and does not
        # commit it, so a fresh clone has a bin pointing at a file that is not
        # there — the single most common way translation failed.
        #
        # Nothing needs that build. esbuild compiles TypeScript directly, so
        # bundling from source produces the same program without running the
        # repository's build script, which would be arbitrary code execution on
        # this server for no gain.
        if entry is not None and not (repo / entry).is_file():
            source = _source_entry(repo, entry)
            if source is not None:
                evidence.append(f"{entry} is a build output; bundling {source} instead")
                entry = source
            else:
                entry = None

        if entry is None:
            found = _source_entry(repo, None)
            if found is not None:
                entry = found
                evidence.append(f"found {found}")

        if entry is None:
            raise BundleError(
                BundleErrorType.ENTRYPOINT_NOT_FOUND,
                "This looks like a Node project but has no source to build from.",
                [
                    "package.json declares no usable entry point, and no "
                    + ", ".join(_ENTRY_CANDIDATES[:4])
                    + " was found.",
                ],
            )

        dependencies = package_json.get("dependencies")
        uses_sdk = isinstance(dependencies, dict) and any(
            name.startswith("@modelcontextprotocol/") for name in dependencies
        )
        if uses_sdk:
            evidence.append("depends on @modelcontextprotocol")

        return Detection(
            runtime="NODE",
            entrypoint=entry,
            evidence=evidence,
            required_environment=_scan_environment(repo),
            # Without the SDK in the dependency list this may be any Node
            # project; the bundle still gets built, and a server that never
            # completes the MCP handshake fails visibly on the device.
            confidence=0.95 if uses_sdk else 0.5,
        )

    for marker, runtime in (
        ("pyproject.toml", "PYTHON"),
        ("requirements.txt", "PYTHON"),
        ("setup.py", "PYTHON"),
        ("build.gradle.kts", "JVM"),
        ("build.gradle", "JVM"),
        ("pom.xml", "JVM"),
    ):
        if (repo / marker).is_file():
            raise BundleError(
                BundleErrorType.RUNTIME_NOT_SUPPORTED,
                f"This is a {runtime.title()} MCP server, which cannot run on a phone.",
                [
                    f"Found {marker}.",
                    "Android has no interpreter for it, and shipping one per server "
                    "would be an interpreter, not a translation.",
                    "Only JavaScript and TypeScript servers can be translated today.",
                ],
            )

    raise BundleError(
        BundleErrorType.NO_DETECTOR_MATCHED,
        "No MCP server was found in that repository.",
        ["Looked for package.json, pyproject.toml, requirements.txt, build.gradle and pom.xml."],
    )


def _esbuild_command() -> list[str]:
    """Locate esbuild.

    Explicit override first, then PATH, then npx as a last resort — npx will
    download, which is fine on a developer machine and wrong in a container,
    so the image installs esbuild and never reaches the fallback.
    """
    override = os.environ.get("ESBUILD_BIN")
    if override:
        return override.split()
    found = shutil.which("esbuild")
    if found:
        return [found]
    if shutil.which("npx"):
        return ["npx", "--yes", "esbuild"]
    raise BundleError(
        BundleErrorType.TOOLCHAIN_UNAVAILABLE,
        "esbuild is not installed on the server.",
        ["Set ESBUILD_BIN or install esbuild on PATH."],
    )


def _npm_diagnosis(output: str) -> list[str]:
    """The lines of npm's output that say what went wrong.

    npm prints the cause first and then several lines of housekeeping — where
    the debug log was written, how to report a bug. Taking the *last* lines,
    which is the obvious way to get "the end of the error", reliably captures
    the housekeeping and discards the diagnosis: a user was shown a path to a
    log file inside a temporary directory that had already been deleted,
    instead of "No matching version found for @claude-flow/mcp@3.0.0-alpha.10".
    """
    useful: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # The trailer, in npm's several spellings across versions.
        if "A complete log of this run" in stripped or "_logs/" in stripped:
            continue
        if stripped.endswith("debug-0.log"):
            continue
        useful.append(stripped)
        if len(useful) >= 4:
            break
    return useful


def install(repo: Path) -> None:
    """Install dependencies, without running the repository's own scripts.

    `--ignore-scripts` is the point of this function. npm lifecycle hooks are
    arbitrary code, and a `postinstall` in a repository someone pasted would
    otherwise execute on this server, as this server, with whatever it can
    reach. Bundling needs the dependency *files*, not their install hooks.
    """
    if not (repo / "package.json").is_file():
        return

    args = ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund", "--loglevel=error"]
    if (repo / "package-lock.json").is_file():
        args = ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--loglevel=error"]

    try:
        result = _run(args, cwd=repo, timeout=INSTALL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise BundleError(
            BundleErrorType.PREPARATION_FAILED,
            "Installing the server's dependencies timed out.",
        ) from exc
    except FileNotFoundError as exc:
        raise BundleError(
            BundleErrorType.TOOLCHAIN_UNAVAILABLE, "npm is not installed on the server."
        ) from exc

    if result.returncode != 0:
        # `npm ci` fails outright when the lockfile disagrees with
        # package.json. That is the repository's problem to fix, but falling
        # back to `npm install` turns a hard stop into a working bundle.
        if args[1] == "ci":
            logger.info("npm ci failed for %s; retrying with npm install", repo.name)
            result = _run(
                [
                    "npm",
                    "install",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    "--loglevel=error",
                ],
                cwd=repo,
                timeout=INSTALL_TIMEOUT_SECONDS,
            )

    if result.returncode != 0:
        raise BundleError(
            BundleErrorType.PREPARATION_FAILED,
            "The server's dependencies could not be installed.",
            _npm_diagnosis(result.stderr or result.stdout or ""),
        )


# Every Node builtin, so an unresolved one can be named as such rather than
# reported as an unresolved package. The list is what `node -p
# "require('module').builtinModules"` prints, minus the ones this bundler
# shims; membership decides *how* a failure is described, never whether it is
# one.
_NODE_BUILTINS = frozenset(
    """assert async_hooks buffer child_process cluster console constants crypto dgram
    diagnostics_channel dns domain fs http http2 https inspector module net os path
    perf_hooks process punycode querystring readline repl stream string_decoder timers
    tls trace_events tty url util v8 vm wasi worker_threads zlib""".split()
)


def _unsupported_from_errors(stderr: str) -> list[str]:
    """Pull the Node builtins out of esbuild's unresolved-import errors.

    Any builtin counts, not only the ones with a hand-written explanation.
    Restricting this to a curated dictionary meant that adding a shim — which
    removes an entry from it — silently downgraded every *other* server that
    needed a different module from "this needs node:crypto and node:stream" to
    "could not be translated", which says nothing a user can act on.
    """
    names: list[str] = []
    for match in re.finditer(r'Could not resolve "(?:node:)?([a-z_0-9/]+)"', stderr):
        name = match.group(1).split("/")[0]
        if name in _NODE_BUILTINS and name not in names:
            names.append(name)
    return names


def _package_identity(repo: Path) -> str:
    """The server's own name and version, as JSON for --define.

    A bundled server has no package.json to read, and several insist on
    reading one — see the note in mcp_host/node/module.mjs. Only these two
    fields are published: the rest of a package.json is dependency and script
    metadata that means nothing once bundled.
    """
    package = _read_json(repo / "package.json") or {}
    return json.dumps(
        {
            "name": str(package.get("name") or "mcp-server"),
            "version": str(package.get("version") or "0.0.0"),
        }
    )


def _seed_files(repo: Path) -> str:
    """Files the virtual filesystem starts with, as JSON for --define.

    Only package.json, at both the paths a bundled server looks for it: beside
    the module and one level up, which is what `join(__dirname, "..")` produces
    from the fictional directory the bundle claims to live in.

    Nothing else is seeded. A server's own source is already inlined, and
    copying a repository's data files into every bundle would put megabytes
    into a download to satisfy a lookup that may never happen.
    """
    package = _read_json(repo / "package.json")
    if package is None:
        return "{}"
    # Re-serialised rather than passed through: the original may hold hundreds
    # of lines of dependency and script metadata that mean nothing here.
    content = json.dumps(
        {
            "name": package.get("name") or "mcp-server",
            "version": package.get("version") or "0.0.0",
            "description": package.get("description") or "",
        }
    )
    return json.dumps({"/package.json": content, "/mcp-server/package.json": content})


def translate(repo: Path, detection: Detection, shims: Path) -> str:
    """Bundle the server into one file the device can evaluate."""
    if detection.runtime != "NODE":
        raise BundleError(
            BundleErrorType.RUNTIME_NOT_SUPPORTED,
            f"{detection.runtime} servers cannot be translated.",
        )

    # esbuild runs with cwd set to the repository, so a relative shim path
    # would resolve inside the cloned repo — where it does not exist, and where
    # a malicious repository could place a file of that name.
    shims = shims.resolve()
    entry = repo / (detection.entrypoint or "")
    if not entry.is_file():
        raise BundleError(
            BundleErrorType.ENTRYPOINT_NOT_FOUND,
            f"The entry point {detection.entrypoint} does not exist in the repository.",
        )

    output = repo / ".creepy-bundle.js"
    args = [
        *_esbuild_command(),
        str(entry),
        "--bundle",
        f"--outfile={output}",
        # esm, not iife, and this is forced rather than chosen. MCP servers
        # end with `await server.connect(...)`, and esbuild refuses top-level
        # await in every format but esm. The output is fully bundled, so it
        # contains no import or export statements — which means the device can
        # wrap it in an async function, where that top-level await is simply an
        # await in an async body. `verify` asserts the no-module-syntax part,
        # because an `export` slipping into the output would break evaluation
        # on the device rather than here.
        "--format=esm",
        # browser, not node: this is what makes esbuild refuse Node builtins
        # instead of marking them external and leaving them to fail on device.
        "--platform=browser",
        # es2022 for top-level await. Hermes supports the syntax this
        # produces once it is inside the async wrapper.
        "--target=es2022",
        "--log-level=warning",
        # --inject, not --alias: `process` is a global in Node, so servers
        # reach it without an import and rewriting specifiers would miss every
        # one of them. --inject binds the shim's exported `process` over the
        # unbound global. `node:process` is aliased as well for the minority
        # that import it explicitly.
        # `import.meta` is module syntax and survives into an ESM bundle, where
        # the device's async-function wrapper makes it a syntax error. Defining
        # the whole object — not just `import.meta.url` — is what replaces the
        # bare references too; defining only the property leaves them behind.
        #
        # The value is a plausible-looking URL because the common use is
        # `fileURLToPath(import.meta.url)` to locate the module. There is no
        # module and no filesystem, so any subsequent file access fails on its
        # own terms rather than on a malformed URL here.
        '--define:import.meta={"url":"file:///mcp-server/index.js"}',
        f"--define:__CREEPY_PACKAGE__={_package_identity(repo)}",
        # An ESM bundle defines neither, and servers use them to find their own
        # package.json — `join(__dirname, "../package.json")` is the usual
        # spelling. Given a directory one level down from the root, that
        # resolves to /package.json, which is where the seed puts it.
        '--define:__dirname="/mcp-server"',
        '--define:__filename="/mcp-server/index.js"',
        f"--define:__CREEPY_SEED_FILES__={_seed_files(repo)}",
        f"--inject:{shims / 'process-shim.mjs'}",
        f"--alias:node:process={shims / 'process-shim.mjs'}",
        f"--alias:process={shims / 'process-shim.mjs'}",
    ]
    args += [f"--alias:{specifier}={shims / 'stdio-shim.mjs'}" for specifier in STDIO_SPECIFIERS]

    # Both spellings: a server may import "path" or "node:path", and esbuild
    # matches the specifier as written.
    node_shims = shims / "node"
    for name in SHIMMED_BUILTINS:
        target = node_shims / f"{name}.mjs"
        args.append(f"--alias:{name}={target}")
        args.append(f"--alias:node:{name}={target}")

    try:
        result = _run(args, cwd=repo, timeout=BUNDLE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise BundleError(BundleErrorType.TRANSLATION_FAILED, "Bundling timed out.") from exc

    if result.returncode != 0:
        unsupported = _unsupported_from_errors(result.stderr or "")
        if unsupported:
            reasons = [
                f"{name} — the server {UNSUPPORTED_BUILTINS[name]}"
                if name in UNSUPPORTED_BUILTINS
                # No hand-written explanation for this one. Naming it is still
                # far more use than not naming it.
                else f"{name} — a part of Node the sandbox does not provide"
                for name in unsupported
            ]
            raise BundleError(
                BundleErrorType.RUNTIME_NOT_SUPPORTED,
                "This server needs parts of Node that a phone does not have: "
                + ", ".join(unsupported)
                + ".",
                reasons,
            )
        detail = [
            line.strip()
            for line in (result.stderr or "").splitlines()
            if line.strip() and not line.startswith(" ")
        ]
        raise BundleError(
            BundleErrorType.TRANSLATION_FAILED,
            "The server could not be translated for the device.",
            detail[:3],
        )

    code = output.read_text(encoding="utf-8")

    # MCP servers are CLI binaries, so their entry file opens with
    # `#!/usr/bin/env node` and esbuild faithfully preserves it. The device
    # wraps the bundle in an async function, where a hashbang is a syntax error
    # — every real server would have failed to evaluate, on the first line,
    # with an error pointing at the wrapper rather than at the cause.
    if code.startswith("#!"):
        newline = code.find("\n")
        code = code[newline + 1 :] if newline >= 0 else ""

    return code


def build(url: str, ref: str | None = None) -> Bundle:
    """Clone, detect, install and translate. The whole pipeline."""
    shims = Path(__file__).parent / "mcp_host"
    workspace = Path(tempfile.mkdtemp(prefix="creepy-mcp-"))
    repo = workspace / "repo"

    try:
        clone(url, ref, repo)
        detection = detect(repo)
        install(repo)
        code = translate(repo, detection, shims)
    finally:
        # The clone holds a stranger's code; it does not outlive the request
        # even when the request failed.
        shutil.rmtree(workspace, ignore_errors=True)

    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    return Bundle(
        bundle_id=digest[:32],
        code=code,
        sha256=digest,
        bytes=len(code.encode("utf-8")),
        detection=detection,
    )
