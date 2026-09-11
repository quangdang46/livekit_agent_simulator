"""Pinned sherpa-onnx model registry for the contract-path TTS backend.

One pinned release (Kitten nano EN fp16, ~25MB) so every machine with the
``tts-sherpa`` extra synthesizes the SAME bytes for the same (voice, text).
Kokoro stays a benchmark candidate (see docs/tts-benchmark.md); promoting
it means adding a second bundle here plus a selection knob — not silently
swapping this default.

Each file carries its own SHA256 (archive layout is fixed upstream):
model + voices + tokens + espeak-ng-data marker. ``ensure_model_dir``
downloads the archive once, verifies every file, and returns the extracted
directory. A mismatch deletes the file/dir and raises
``ModelIntegrityError`` (the contract path treats that as "fall back to OS
TTS", never as silent corruption).
"""

from __future__ import annotations

import hashlib
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ModelFile:
    """One file inside the extracted model directory."""

    relpath: str
    sha256: str


@dataclass(frozen=True)
class PinnedModelBundle:
    """One pinned model release: archive URL + per-file digests."""

    model_id: str
    archive_url: str
    archive_sha256: str
    archive_filename: str
    extract_dirname: str
    files: tuple[ModelFile, ...] = field(default_factory=tuple)


_KITTEN_NANO_EN_V0_1_FP16 = PinnedModelBundle(
    model_id="kitten-nano-en-v1",
    archive_url=(
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
        "tts-models/kitten-nano-en-v0_1-fp16.tar.bz2"
    ),
    # GPL-3.0 archive (~25.6MB, 26,836,277 bytes). SHA measured 2026-09-11
    # via shasum of the release download.
    archive_sha256="f35dac93754fe2ac97c66e1f468311d0d2130f7f0f5a89bfa1197e09a0cbdec5",
    archive_filename="kitten-nano-en-v0_1-fp16.tar.bz2",
    extract_dirname="kitten-nano-en-v0_1-fp16",
    files=(
        # Measured 2026-09-11 from the extracted archive contents.
        ModelFile(
            relpath="model.fp16.onnx",
            sha256="6b42d25df767db408d95738b464f02168a9cfb76367c1b2b9e90095485981407",
        ),
        ModelFile(
            relpath="voices.bin",
            sha256="138cf3a7afd0ebf1f9d6fb72f49e960ef8405252eaff5d130cf3fba1b038a741",
        ),
        ModelFile(
            relpath="tokens.txt",
            sha256="934a4188addc7665dd3410256bb622169242357fbb99d840d9351209b486dabb",
        ),
        ModelFile(
            relpath="README.md",
            sha256="e8751a029481521364265c7c95acf0394fa3580671f19e99c1e9ce51c74ba9d6",
        ),
        ModelFile(
            relpath="LICENSE",
            sha256="cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
        ),
    ),
)

_DEFAULT_VOICE = "af_heart"
_DEFAULT_LANGUAGE = "en-US"


def default_model_bundle() -> PinnedModelBundle:
    """The production pinned model bundle for contract-path TTS."""
    return _KITTEN_NANO_EN_V0_1_FP16


def default_voice() -> tuple[str, str]:
    """(voice, language) defaults for contract-path synthesis."""
    return _DEFAULT_VOICE, _DEFAULT_LANGUAGE


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_members(tar: tarfile.TarFile, dest: Path) -> list[tarfile.TarInfo]:
    """Return archive members only if every one extracts inside ``dest``.

    Defense-in-depth hygiene on top of the archive-SHA check: a pinned,
    hash-verified archive cannot be tampered with, but a member with an
    absolute path (``/etc/x``), a ``..`` escape, or a symlink/hardlink
    target would still write outside ``dest``. Reject the whole archive
    instead (fail closed via ``ModelIntegrityError``, same as a SHA
    mismatch) so extraction can never escape the cache dir.

    Explicit member check rather than ``extractall(filter=...)`` because
    the package floor is Python 3.10 and the ``filter`` parameter only
    exists on 3.12+.
    """
    from .sherpa_tts import ModelIntegrityError

    dest_resolved = dest.resolve()
    members = tar.getmembers()
    for member in members:
        if member.issym() or member.islnk():
            raise ModelIntegrityError(
                f"model archive member {member.name!r} is a link; refusing extraction"
            )
        target = (dest_resolved / member.name).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError:
            raise ModelIntegrityError(
                f"model archive member {member.name!r} escapes the cache dir; "
                "refusing extraction"
            ) from None
    return members


def ensure_model_dir(
    bundle: PinnedModelBundle,
    cache_dir: Path,
    *,
    downloader: Callable[[str, Path], None],
) -> Path:
    """Download (once) + verify the pinned bundle; return the model dir.

    Idempotent: when every pinned file exists AND verifies, no download
    happens. Corrupt/mismatched files are deleted and raise
    ``ModelIntegrityError``. ``downloader`` is injected so tests never
    touch the network.
    """
    from .sherpa_tts import ModelIntegrityError

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    model_dir = cache_dir / bundle.extract_dirname

    def _verified() -> bool:
        for entry in bundle.files:
            candidate = model_dir / entry.relpath
            if not candidate.is_file() or _sha256_file(candidate) != entry.sha256:
                return False
        data_dir = model_dir / "espeak-ng-data"
        return data_dir.is_dir() and any(data_dir.iterdir())

    if _verified():
        return model_dir

    # A present-but-unverifiable model dir fails closed immediately: never
    # re-download over tampered files as if they were merely absent.
    if model_dir.exists():
        raise ModelIntegrityError(
            f"model {bundle.model_id!r} files failed SHA256 verification"
        )

    archive = cache_dir / bundle.archive_filename
    if not archive.is_file():
        downloader(bundle.archive_url, archive)
    if _sha256_file(archive) != bundle.archive_sha256:
        archive.unlink(missing_ok=True)
        raise ModelIntegrityError(
            f"model {bundle.model_id!r} archive failed SHA256 verification"
        )
    import shutil

    if model_dir.exists():
        shutil.rmtree(model_dir)
    with tarfile.open(archive, "r:*") as tar:
        tar.extractall(cache_dir, members=_validated_members(tar, cache_dir))
    if not _verified():
        raise ModelIntegrityError(
            f"model {bundle.model_id!r} extracted files failed SHA256 verification"
        )
    return model_dir


# Back-compat alias: the pre-bundle registry exposed a single-file spec.
# Kept so live_wiring/tests importing default_model_spec keep working until
# the bundle migration below lands there too.
def default_model_spec():  # pragma: no cover - transitional shim
    from .sherpa_tts import PinnedModelSpec

    bundle = default_model_bundle()
    onnx = next(f for f in bundle.files if f.relpath.endswith(".onnx"))
    return PinnedModelSpec(
        model_id=bundle.model_id,
        url=bundle.archive_url,
        sha256=bundle.archive_sha256,
        filename=bundle.archive_filename,
    )


from .sherpa_tts import ModelIntegrityError  # noqa: E402,F401 — re-exported for callers

__all__ = [
    "ModelFile",
    "ModelIntegrityError",
    "PinnedModelBundle",
    "default_model_bundle",
    "default_model_spec",
    "default_voice",
    "ensure_model_dir",
]
