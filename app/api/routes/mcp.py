"""Translating MCP servers so they can run on the phone.

An MCP server is normally a process: spawned, wired to stdin and stdout,
importing its language's standard library. Android gives an app no way to exec
an interpreter it did not ship, so none of that is available on a device.

This endpoint does the part that needs a real machine — clone, detect, install,
bundle — and hands back a single JavaScript file. The server then runs *on the
device*, inside the app's own runtime. Nothing is proxied back through here at
call time: this is a compiler, not a host, and once a bundle is downloaded the
server keeps working with the backend unreachable.

The split matters for what the user asked for. Every MCP server stays local to
the phone; the only thing that happens off-device is the translation, once, at
the moment a server is added.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import Field

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.db.models import User
from app.schemas.common import CamelModel
from app.services.mcp_bundler import BundleError, build

logger = logging.getLogger("app.mcp")

router = APIRouter(prefix="/mcp", tags=["mcp"])


class TranslateRequest(CamelModel):
    url: str = Field(description="https repository URL of the MCP server.")
    ref: str | None = Field(default=None, description="Branch, tag or commit.")


class EnvironmentVariableResponse(CamelModel):
    name: str
    required: bool
    description: str | None = None


class TranslateResponse(CamelModel):
    bundle_id: str
    runtime: str
    entrypoint: str | None
    confidence: float
    evidence: list[str]
    required_environment: list[EnvironmentVariableResponse]
    sha256: str
    bytes: int
    download_url: str


def _bundle_root(request: Request) -> Path:
    root = Path(request.app.state.settings.mcp_bundle_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


@router.post("/translate")
async def translate_server(
    request: Request,
    body: TranslateRequest,
    _user: User = Depends(get_current_user),
) -> TranslateResponse:
    try:
        bundle = build(body.url, body.ref)
    except BundleError as exc:
        # The bundler's own vocabulary reaches the app unchanged: it
        # distinguishes "this is a Python server" from "this repository does
        # not exist", and flattening both to 400 would lose the only part the
        # user can act on.
        raise ApiError(
            422,
            exc.type,
            str(exc),
            # Hints carry the detector's evidence and the compiler's real
            # complaint. They are the difference between "translation failed"
            # and "this server reads files, which it cannot do on a phone".
            extra={"hints": exc.hints},
        ) from exc

    path = _bundle_root(request) / f"{bundle.bundle_id}.js"
    # Content-addressed, so an identical translation is written once and a
    # second request for the same commit is free.
    if not path.exists():
        path.write_text(bundle.code, encoding="utf-8")

    logger.info(
        "translated mcp server url=%s runtime=%s bytes=%d",
        body.url,
        bundle.detection.runtime,
        bundle.bytes,
    )

    return TranslateResponse(
        bundle_id=bundle.bundle_id,
        runtime=bundle.detection.runtime,
        entrypoint=bundle.detection.entrypoint,
        confidence=bundle.detection.confidence,
        evidence=bundle.detection.evidence,
        required_environment=[
            EnvironmentVariableResponse(
                name=variable.name,
                required=variable.required,
                description=variable.description,
            )
            for variable in bundle.detection.required_environment
        ],
        sha256=bundle.sha256,
        bytes=bundle.bytes,
        download_url=f"/mcp/bundles/{bundle.bundle_id}",
    )


@router.get("/bundles/{bundle_id}")
async def download_bundle(
    request: Request,
    bundle_id: str,
    _user: User = Depends(get_current_user),
) -> FileResponse:
    # The id is a hex digest by construction, and it is checked rather than
    # trusted: it is about to become a path segment, and "../" is also a string.
    if not bundle_id or len(bundle_id) > 64 or not all(c in "0123456789abcdef" for c in bundle_id):
        raise ApiError(404, "BUNDLE_NOT_FOUND", "No such bundle.")

    path = _bundle_root(request) / f"{bundle_id}.js"
    if not path.is_file():
        raise ApiError(404, "BUNDLE_NOT_FOUND", "No such bundle.")

    return FileResponse(
        path,
        media_type="application/javascript",
        headers={
            # The device verifies what it got before evaluating it. A bundle is
            # executable code, so "probably the right bytes" is not good enough.
            "x-bundle-sha256": bundle_id,
            "cache-control": "public, max-age=31536000, immutable",
        },
    )
