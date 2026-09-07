#!/usr/bin/env python3
"""Generate the two secrets that device licensing needs.

    LICENSE_SIGNING_KEY  RSA private key, PEM. Signs licences (RS256).
    MODEL_KEYS_JSON      {"<modelVersion>": "<base64 AES-256 key>"}

Run it once per environment:

    python scripts/generate_license_keys.py

Both land in ``secrets/`` (already gitignored) and the matching ``.env`` lines
are printed for pasting.

Three things this script is careful about, because each of them is a mistake
that is silent until it is expensive:

* **It refuses to overwrite.** Regenerating the signing key invalidates every
  licence already issued; regenerating a model key makes every encrypted shard
  already on a user's phone permanently undecryptable, because the shards were
  encrypted with the old key and nothing re-encrypts them. Use --force only
  when you mean exactly that, and --add-version to add a model without
  disturbing the existing ones.

* **It emits the public half too.** The app verifies licences, so it needs
  ``license-public-key.pem`` bundled. Generating a private key and discovering
  a week later that nothing can check its signatures is the usual way this
  goes wrong.

* **The private key is written 0600 and never printed in full.** The .env line
  is printed because you need it; the file is the copy that matters.

A note the config file also makes, worth repeating here: an env var holding a
private key is visible in ``docker inspect``, in ``/proc/<pid>/environ`` and in
crash dumps. The PEM file this writes is the safer artifact of the two.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
from pathlib import Path
from typing import NoReturn

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# 3072 bits rather than 2048: this key signs every licence for the lifetime of
# the deployment and rotating it invalidates all of them, so the cost of the
# larger key (a slightly longer signature) is paid once and the benefit lasts.
KEY_SIZE = 3072

# AES-256. The model shards are encrypted with this; 32 bytes is what a
# streaming AES-GCM reader in the app will expect.
MODEL_KEY_BYTES = 32

DEFAULT_VERSIONS = ("gui-owl-2b-v1",)


def _die(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _write_private(path: Path, data: bytes) -> None:
    """Create 0600 from the start, not chmod-after.

    Writing then chmod-ing leaves a window in which the key is world-readable,
    which on a shared build host is a real window.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _env_line(name: str, value: str) -> str:
    """A .env line for a value that may contain newlines.

    python-dotenv (which pydantic-settings uses) unescapes ``\\n`` inside
    double quotes, so a PEM survives the round trip on one line. Backslashes
    and quotes are escaped first, or a value containing either would truncate
    the line and produce a key that parses as garbage.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'{name}="{escaped}"'


def generate_signing_key(out_dir: Path, *, force: bool) -> str:
    private_path = out_dir / "license-signing-key.pem"
    public_path = out_dir / "license-public-key.pem"

    if private_path.exists() and not force:
        _die(
            f"{private_path} already exists. Overwriting it invalidates every "
            "licence already issued; pass --force if that is what you want."
        )

    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    _write_private(private_path, private_pem)
    public_path.write_bytes(public_pem)

    print(f"wrote {private_path}  (0600 — the signing key)")
    print(f"wrote {public_path}  (bundle this in the app to verify licences)")
    return private_pem.decode()


def generate_model_keys(
    out_dir: Path,
    versions: tuple[str, ...],
    *,
    force: bool,
    add: bool,
) -> str:
    path = out_dir / "model-keys.json"
    existing: dict[str, str] = {}

    if path.exists():
        if not (force or add):
            _die(
                f"{path} already exists. Replacing a model key makes shards "
                "already on devices undecryptable; pass --add-version to add a "
                "model without touching the others, or --force to replace all."
            )
        if add:
            try:
                existing = json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                _die(f"{path} is not valid JSON ({exc}); refusing to rewrite it.")
            if not isinstance(existing, dict):
                _die(f"{path} is not a JSON object; refusing to rewrite it.")

    for version in versions:
        if version in existing:
            # Silently regenerating here is the worst outcome: it looks like
            # success and breaks every install already carrying that version.
            _die(
                f"{version} already has a key in {path}. Retire it with a new "
                "version id instead of replacing it in place."
            )
        existing[version] = base64.b64encode(secrets.token_bytes(MODEL_KEY_BYTES)).decode()

    payload = json.dumps(existing, indent=2, sort_keys=True) + "\n"
    _write_private(path, payload.encode())

    print(f"wrote {path}  (0600 — {len(existing)} model key(s))")
    for version in sorted(existing):
        marker = "new" if version in versions else "kept"
        print(f"    {version}  [{marker}]")

    return json.dumps(existing, separators=(",", ":"), sort_keys=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out-dir",
        default="secrets",
        help="Where the key files go (default: secrets/, which is gitignored).",
    )
    parser.add_argument(
        "--model-version",
        action="append",
        dest="versions",
        metavar="ID",
        help=(
            "Model version to mint a key for; repeatable. "
            f"Default: {', '.join(DEFAULT_VERSIONS)}"
        ),
    )
    parser.add_argument(
        "--add-version",
        action="store_true",
        help="Add the given versions to an existing model-keys.json.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing keys. Invalidates issued licences and shipped shards.",
    )
    args = parser.parse_args()

    versions = tuple(args.versions or DEFAULT_VERSIONS)
    if len(set(versions)) != len(versions):
        _die("the same model version was given twice")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []

    # --add-version touches only the model keys: rotating the signing key is a
    # separate, much more disruptive act and should never be a side effect.
    if args.add_version:
        model_keys = generate_model_keys(out_dir, versions, force=False, add=True)
        lines.append(_env_line("MODEL_KEYS_JSON", model_keys))
    else:
        signing_key = generate_signing_key(out_dir, force=args.force)
        model_keys = generate_model_keys(out_dir, versions, force=args.force, add=False)
        lines.append(_env_line("LICENSE_SIGNING_KEY", signing_key))
        lines.append(_env_line("MODEL_KEYS_JSON", model_keys))

    if args.add_version:
        print("\nReplace MODEL_KEYS_JSON in .env with:\n")
    else:
        print("\nAdd to .env (or your secret store):\n")
    for line in lines:
        print(line)
    if not args.add_version:
        print(
            "\nUntil both are set, POST /device/license answers 503 rather "
            "than failing open."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
