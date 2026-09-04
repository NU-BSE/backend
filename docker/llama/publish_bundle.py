#!/usr/bin/env python3
"""Publish converted GGUFs where /models/catalog can find them.

Conversion writes a flat directory that llama-server reads directly. The
catalogue reads a different shape — ``<root>/<model>/artifacts/gguf/`` with an
export manifest naming every file's size and SHA-256 — because the app verifies
what it downloaded before running it, and hashing a gigabyte per request to
produce that would be absurd.

Bridging the two is this script. Files are hard-linked rather than copied: the
same bytes, in two places, at no extra cost on the same filesystem, and a copy
of 1.8 GB per conversion would be pure waste.

The manifest is written last and atomically. A catalogue that read a
half-written manifest would publish sizes that do not describe the bytes it
serves, and every client would fail verification *after* downloading a
gigabyte — the most expensive possible moment to discover it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

CHUNK = 8 * 1024 * 1024


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def link_or_copy(source: Path, destination: Path) -> None:
    if destination.exists():
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        # A different filesystem, or a filesystem without hard links. Copying
        # is slower but correct, and this is the fallback rather than the path.
        destination.write_bytes(source.read_bytes())


def main(source_dir: str, root: str, model: str, name: str) -> int:
    # An empty root is what `"$MODEL_ARTIFACT_ROOT"` expands to when the
    # variable is not set, and Path("") is ".", so this would publish into the
    # current directory and report success. The catalogue would go on finding
    # nothing, with no error anywhere to explain it.
    if not root.strip():
        print(
            "[publish] FATAL: the destination root is empty. Pass the same "
            "path the API reads as MODEL_ARTIFACT_ROOT (default: app/models).",
            file=sys.stderr,
        )
        return 2

    source = Path(source_dir)
    target = Path(root) / model / "artifacts" / "gguf"
    target.mkdir(parents=True, exist_ok=True)

    published: dict[str, dict[str, object]] = {}
    for path in sorted(source.glob("*.gguf")):
        # The un-quantized text model is an intermediate: several times the
        # bytes for something the phone cannot run. The projector keeps its
        # f16 precision and does ship.
        if path.name.endswith(("-f16.gguf", "-bf16.gguf")) and "mmproj" not in path.name:
            continue
        destination = target / path.name
        link_or_copy(path, destination)
        published[path.name] = {
            "bytes": destination.stat().st_size,
            "sha256": sha256_of(destination),
        }
        print(f"[publish] {path.name} ({published[path.name]['bytes']:,} bytes)", file=sys.stderr)

    if not published:
        print(f"[publish] FATAL: no shippable .gguf found in {source}", file=sys.stderr)
        return 1

    manifest = target / f"{name}-export-manifest.json"
    temporary = manifest.with_suffix(".json.partial")
    temporary.write_text(json.dumps({"files": published, "student": model}, indent=2))
    temporary.replace(manifest)
    print(f"[publish] manifest {manifest}", file=sys.stderr)
    # The catalogue is built once, at API startup: the manifests do not change
    # while the process runs, so a publish into a live deployment is invisible
    # until it restarts. Saying so here is cheaper than the alternative, which
    # is someone concluding the publish did not work.
    print(
        "[publish] done. Restart the API — the catalogue is read at startup.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("usage: publish_bundle.py <source-dir> <root> <model> <name>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(*sys.argv[1:]))
