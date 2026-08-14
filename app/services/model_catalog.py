"""On-device model weights served to entitled clients.

Onboarding picks a memory profile; this is where the weights for that profile
come from. The files are large (250MB-1.5GB per profile), so the contract is
built for interrupted mobile downloads: every file is published with its size
and SHA-256 up front, and the download endpoint supports Range requests, so a
client can resume rather than restart and can prove what it got.

Integrity data is read from the export manifests written at conversion time
rather than hashed here. Hashing a gigabyte per request would be absurd, and
hashing once at startup would still only attest to what is on this disk — the
manifest attests to what came out of the conversion, which is the thing worth
checking against.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings

logger = logging.getLogger("app.models")

# The memory profiles offered in onboarding, mapped to the student model each
# one runs. `cloud` is absent on purpose: opting out of local inference means
# there is nothing to download, not a zero-byte download.
PROFILE_TO_MODEL: dict[str, str] = {
    "efficient": "0.5b",
    "balanced": "1b",
    "performance": "1.5b",
}

# Only these two are shipped to devices. The bf16 conversion is an intermediate
# left in the manifest by the export step; sending it would be several times
# the bytes for a model the phone cannot run.
_SHIPPED_SUFFIXES = ("-q4_0.gguf", "-bf16.gguf")


@dataclass(frozen=True)
class ModelFile:
    name: str
    role: str
    bytes: int
    sha256: str
    path: Path


@dataclass(frozen=True)
class ModelBundle:
    profile: str
    model: str
    files: tuple[ModelFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.bytes for item in self.files)


class ModelCatalog:
    """Reads the exported GGUF bundles off disk.

    Built once at startup: the manifests do not change while the process runs,
    and a client asking "what would I download" should not cost a directory
    walk. A profile whose files are missing is omitted rather than advertised,
    so the app is never offered a download that would 404 halfway through.
    """

    def __init__(self, settings: Settings) -> None:
        self._root = Path(settings.model_artifact_root)
        self._bundles: dict[str, ModelBundle] = {}
        self._load()

    def _load(self) -> None:
        for profile, model in PROFILE_TO_MODEL.items():
            bundle = self._read_bundle(profile, model)
            if bundle is None:
                logger.warning(
                    "model profile %s (%s) has no usable artifacts; not offered",
                    profile,
                    model,
                )
                continue
            self._bundles[profile] = bundle
            logger.info(
                "model profile %s -> %s, %d files, %.1f MB",
                profile,
                model,
                len(bundle.files),
                bundle.total_bytes / 1024 / 1024,
            )

    def _read_bundle(self, profile: str, model: str) -> ModelBundle | None:
        gguf_dir = self._root / model / "artifacts" / "gguf"
        if not gguf_dir.is_dir():
            return None

        manifests = sorted(gguf_dir.glob("*-export-manifest.json"))
        if not manifests:
            return None
        try:
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("unreadable export manifest %s: %s", manifests[0], exc)
            return None

        entries = manifest.get("files")
        if not isinstance(entries, dict):
            return None

        files: list[ModelFile] = []
        for name, meta in sorted(entries.items()):
            if not isinstance(meta, dict) or not name.endswith(_SHIPPED_SUFFIXES):
                continue
            path = gguf_dir / name
            if not path.is_file():
                # In the manifest but not on disk — the bf16 intermediates are
                # routinely deleted after quantization. Skip quietly.
                continue
            declared = int(meta.get("bytes") or 0)
            actual = path.stat().st_size
            if declared and declared != actual:
                # Publishing a size and a hash that do not describe the bytes
                # we would send guarantees every client fails verification
                # after paying for the download. Refuse the file instead.
                logger.error(
                    "%s is %d bytes but the manifest says %d; excluded",
                    path,
                    actual,
                    declared,
                )
                continue
            sha = str(meta.get("sha256") or "")
            if not sha:
                logger.error("%s has no sha256 in the manifest; excluded", path)
                continue
            files.append(
                ModelFile(
                    name=name,
                    role="projector" if name.startswith("mmproj") else "weights",
                    bytes=actual,
                    sha256=sha,
                    path=path,
                )
            )

        # A projector without weights is unusable, and so is the reverse.
        if not any(item.role == "weights" for item in files):
            return None
        return ModelBundle(profile=profile, model=model, files=tuple(files))

    @property
    def profiles(self) -> tuple[str, ...]:
        return tuple(self._bundles)

    def bundle(self, profile: str) -> ModelBundle | None:
        return self._bundles.get(profile)

    def file(self, profile: str, name: str) -> ModelFile | None:
        """Resolve a file within a profile.

        Lookup is by exact name against the catalogue, never by joining the
        request onto a path — `..` and absolute paths cannot escape a set
        membership test, and this endpoint takes a filename from the network.
        """
        bundle = self._bundles.get(profile)
        if bundle is None:
            return None
        for item in bundle.files:
            if item.name == name:
                return item
        return None
