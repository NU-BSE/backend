#!/usr/bin/env python3
"""Report what /models/catalog can see, and why anything is missing.

Run on the server when the app says the model cannot be downloaded. The
catalogue's own logic is deliberately quiet — a profile with no usable
artifacts is skipped rather than advertised, which is right for serving and
useless for diagnosing.

    python3 scripts/check_model_catalog.py

Every check prints its answer, including the ones that pass, so the first
failing line is the whole diagnosis.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings
from app.services.model_catalog import (
    PROFILE_TO_MODEL,
    ModelCatalog,
    _is_projector,
    _is_shipped,
)


def main() -> int:
    settings = Settings()
    root = Path(settings.model_artifact_root)

    print(f"MODEL_ARTIFACT_ROOT : {settings.model_artifact_root}")
    print(f"  resolved          : {root.resolve()}")
    print(f"  exists            : {root.is_dir()}")
    if not root.is_dir():
        print("\nThe root does not exist. Publish into it, or set")
        print("MODEL_ARTIFACT_ROOT to wherever the weights actually are.")
        return 1

    problems = 0
    for profile, model in PROFILE_TO_MODEL.items():
        gguf = root / model / "artifacts" / "gguf"
        print(f"\nprofile {profile!r} -> model {model!r}")
        print(f"  looking in        : {gguf}")

        if not gguf.is_dir():
            print("  MISSING           : that directory does not exist.")
            # The most common mistake: the files are somewhere, just not in the
            # layout the catalogue reads. Point at them if they can be found.
            found = sorted(root.rglob("*.gguf"))[:5] or sorted(
                Path.cwd().rglob("*.gguf")
            )[:5]
            if found:
                print("  but .gguf files exist at:")
                for item in found:
                    print(f"      {item}")
                print("  -> publish_bundle.py moves them into the layout above.")
            problems += 1
            continue

        manifests = sorted(gguf.glob("*-export-manifest.json"))
        if not manifests:
            print("  MISSING           : no *-export-manifest.json.")
            print("  -> publish_bundle.py writes one with each file's size and digest.")
            problems += 1
            continue
        print(f"  manifest          : {manifests[0].name}")

        try:
            entries = json.loads(manifests[0].read_text()).get("files") or {}
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  UNREADABLE        : {exc}")
            problems += 1
            continue

        shipped = 0
        for name, meta in sorted(entries.items()):
            path = gguf / name
            role = "projector" if _is_projector(name) else "weights"
            if not _is_shipped(name):
                print(f"    {name}: excluded (intermediate, not shipped)")
                continue
            if not path.is_file():
                print(f"    {name}: IN MANIFEST BUT NOT ON DISK")
                problems += 1
                continue
            declared = int(meta.get("bytes") or 0)
            actual = path.stat().st_size
            if declared and declared != actual:
                print(f"    {name}: SIZE MISMATCH manifest {declared} vs disk {actual}")
                problems += 1
                continue
            if not meta.get("sha256"):
                print(f"    {name}: NO sha256 IN MANIFEST")
                problems += 1
                continue
            print(f"    {name}: ok, {actual:,} bytes, role={role}")
            shipped += 1

        if shipped == 0:
            print("  RESULT            : nothing shippable; this profile is not offered.")
            problems += 1

    print("\n--- what the running API would serve ---")
    catalog = ModelCatalog(settings)
    if not catalog.profiles:
        print("  no profiles. /models/catalog returns bundles: []")
        print("  which the app shows as 'no weights published for this profile'.")
        problems += 1
    for profile in catalog.profiles:
        bundle = catalog.bundle(profile)
        if bundle is None:
            continue
        print(f"  {profile}: {len(bundle.files)} file(s), {bundle.total_bytes:,} bytes")
        for item in bundle.files:
            print(f"      {item.name}  role={item.role}")

    if problems == 0:
        print("\nThe catalogue is fine. If the app still says otherwise:")
        print("  * the API builds this once at startup — restart it;")
        print("  * /models/catalog also requires an entitlement unless")
        print("    MODEL_DOWNLOAD_REQUIRES_ENTITLEMENT=false.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
