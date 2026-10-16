"""Fetch + cache the SWM test tunes on demand, SHA-256 verified, never committed.

CSDB sources: the SID-Wizard 1.94 source tarball (four player-test tunes, one SWP) and
the SID-Wizard 1.7 smallpack (quickscore.swm). Any SHA-256 mismatch is a hard error.
Test-only module, imported only from ``tests/``.
"""

from __future__ import annotations

import hashlib
import os
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, Tuple

TARBALL_URL = "https://csdb.dk/getinternalfile.php/276275/SID-Wizard-1.94-with-sources.tar.gz"
TARBALL_SHA256 = "544e36aff3fe14b7e4cf81a04c680a6883191a222754b2f0489e15349a89b559"
_TARBALL_NAME = "SID-Wizard-1.94-with-sources.tar.gz"

# Tune name -> (tarball member path, expected SHA-256 of the SWM bytes).
TUNE_MEMBERS: Dict[str, Tuple[str, str]] = {
    "flashitback": (
        "SID-Wizard-1.94/native/examples/flashitback.swm",
        "53fc4da79248737e28e04caa692c0794653b5db0f3ea0ee3d8917307d57cee60",
    ),
    "bronkosaurus": (
        "SID-Wizard-1.94/native/examples/bronkosaurus.swm",
        "96862c8fddf77cf3abbf2ff2010b761bcef4ea0968b5843a9408d1aec99e3591",
    ),
    "euphoria": (
        "SID-Wizard-1.94/native/examples/euphoria.swm",
        "5f7103f65288bbeb39200aa6f553f2e747742de96dd17edc0ff7675fd8264f7d",
    ),
    "rain8580": (
        "SID-Wizard-1.94/native/examples/rain8580.swm",
        "edbd461c0f1d92a18420589b86dcefb685e748e7be68b390681d68030fcafcaf",
    ),
}

TUNE_NAMES: Tuple[str, ...] = tuple(TUNE_MEMBERS.keys())

# Packed ``SWP`` music-data files from the tarball, keyed by cache file name.
SWP_FILES: Dict[str, Tuple[str, str]] = {
    "flashitback.swp": (
        "SID-Wizard-1.94/native/sources/SWP-slowdown-example/flashitback.swp",
        "ce7180b2dcb0902d760a40968354af35bf59c983a9ba34ba36a6cb360afd1dbe",
    ),
}

SWP_NAMES: Tuple[str, ...] = tuple(SWP_FILES.keys())

# quickscore.swm — all nine instruments store first-waveform byte $00 (the SWM1 $09-at-load default).
SW17_ZIP_URL = "https://csdb.dk/getinternalfile.php/131083/SID-Wizard-1.7-smallpack.zip"
SW17_ZIP_SHA256 = "c7fabb73aefa5a779d9ced7a661258c33e97ac4ee42bea22fa5ae8ee6e55167f"
QUICKSCORE_MEMBER = "SID-Wizard-1.7-smallpack/demotunes/didnt-fit-on-disk/quickscore.swm"
QUICKSCORE_SHA256 = "52cf7c8662aa297e7306b421f4966c5a5bacb79c5dd6dee79dca9a704d2329f0"

# issue #57: editor-saved modules that reference an instrument above the count, all in the 1.94 tarball.
OOB_INST_TUNE_MEMBERS: Dict[str, Tuple[str, str]] = {
    "vn-drdudas": (
        "SID-Wizard-1.94/native/examples/swm-more/vn-drdudas.swm",
        "b7af603d90a8ffdda02842138869e7c7734820688a8e356f4eb926b19fca2d9b",
    ),
    "vn-pimasz": (
        "SID-Wizard-1.94/native/examples/swm-more/vn-pimasz.swm",
        "4ad6a49b325ea4e4f250f75e9cfb4b1cafbc91748b0187ad7dc6d786ba309887",
    ),
    "np-sw19-emu": (
        "SID-Wizard-1.94/resources/examples/np-sw19-emu.swm",
        "fddde8c65f0f57154023bf4cd2218346db1e432af4a10a1427cd2ec8a4e18d33",
    ),
    "gurza-dream": (
        "SID-Wizard-1.94/resources/examples/gurza-dream.swm",
        "aafa3f8fffc40ce3d1104a97ebefee9eb81248bac525a314cc361a63486cc23b",
    ),
    "uraniumfever": (
        "SID-Wizard-1.94/resources/examples/uraniumfever.swm",
        "2581f052f7b13675d05c9b8237ab5b339d845044ea646b5b4e9c0caba629edf0",
    ),
}

OOB_INST_TUNE_NAMES: Tuple[str, ...] = tuple(OOB_INST_TUNE_MEMBERS.keys())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def swm_cache_dir() -> Path:
    """Local cache directory for the tarball and extracted SWM bytes."""
    env = os.environ.get("SIDWIZARD_TUNECACHE")
    out = Path(env) if env else Path(__file__).resolve().parent / ".swmcache"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _atomic_write(dest: Path, data: bytes) -> None:
    """Write ``data`` to ``dest`` atomically so a concurrent reader never
    observes a half-written file: write to a temp file in the same dir and
    ``os.replace()`` it into place (atomic on same-fs, xdist-safe)."""
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as fp:
            fp.write(data)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, dest)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _download(url: str, *, retries: int = 4) -> bytes:
    """Download ``url``, retrying transient failures with exponential backoff."""
    req = urllib.request.Request(url, headers={"User-Agent": "pysidwizard/fetch"})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310 (https)
                return resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = exc
        if attempt + 1 < retries:
            time.sleep(min(2**attempt, 5))
    raise RuntimeError(f"{url}: unreachable after {retries} attempts ({last_err})")


def _tarball_path() -> Path:
    """Ensure the SID-Wizard 1.94 source tarball is downloaded + SHA-256
    verified, then return its cached filesystem path.

    Atomic ``os.replace`` makes this xdist-safe without a lock: a worker
    that loses the race simply finds the verified tarball already in place."""
    tarball = swm_cache_dir() / _TARBALL_NAME
    if tarball.is_file() and _sha256(tarball) == TARBALL_SHA256:
        return tarball
    data = _download(TARBALL_URL)
    got = hashlib.sha256(data).hexdigest()
    if got != TARBALL_SHA256:
        raise RuntimeError(
            f"{_TARBALL_NAME} SHA-256 mismatch: got {got}, expected {TARBALL_SHA256}"
        )
    _atomic_write(tarball, data)
    return tarball


def swm_path(tune: str) -> Path:
    """Return a filesystem path to ``{tune}.swm``, fetching + extracting
    on first call. SHA-256 verified against the expected SID-Wizard 1.94
    bytes. Idempotent and safe under ``pytest-xdist`` (writes are atomic)."""
    if tune not in TUNE_MEMBERS:
        raise KeyError(f"unknown tune {tune!r}; expected one of {TUNE_NAMES}")
    member, expected_sha = TUNE_MEMBERS[tune]
    dest = swm_cache_dir() / f"{tune}.swm"
    # Fast path: already extracted + verified.
    if dest.is_file() and _sha256(dest) == expected_sha:
        return dest

    tarball = _tarball_path()
    with tarfile.open(tarball, "r:gz") as tf:
        src = tf.extractfile(member)
        if src is None:
            raise RuntimeError(f"tarball member {member!r} is not a regular file")
        data = src.read()

    got = hashlib.sha256(data).hexdigest()
    if got != expected_sha:
        raise RuntimeError(f"{tune}.swm SHA-256 mismatch: got {got}, expected {expected_sha}")
    _atomic_write(dest, data)
    return dest


def all_swm_paths() -> Dict[str, Path]:
    """Materialise all four tunes; return ``{tune: path}``."""
    return {tune: swm_path(tune) for tune in TUNE_NAMES}


def oob_inst_path(name: str) -> Path:
    """Issue #57 fixture (instrument ref above the count); SHA-256 verified, xdist-safe."""
    if name not in OOB_INST_TUNE_MEMBERS:
        raise KeyError(f"unknown fixture {name!r}; expected one of {OOB_INST_TUNE_NAMES}")
    member, expected_sha = OOB_INST_TUNE_MEMBERS[name]
    dest = swm_cache_dir() / f"{name}.swm"
    # Fast path: already extracted + verified.
    if dest.is_file() and _sha256(dest) == expected_sha:
        return dest
    tarball = _tarball_path()
    with tarfile.open(tarball, "r:gz") as tf:
        src = tf.extractfile(member)
        if src is None:
            raise RuntimeError(f"tarball member {member!r} is not a regular file")
        data = src.read()
    got = hashlib.sha256(data).hexdigest()
    if got != expected_sha:
        raise RuntimeError(f"{name}.swm SHA-256 mismatch: got {got}, expected {expected_sha}")
    _atomic_write(dest, data)
    return dest


def all_oob_inst_paths() -> Dict[str, Path]:
    """Materialise all issue #57 fixtures; return ``{name: path}``."""
    return {name: oob_inst_path(name) for name in OOB_INST_TUNE_NAMES}


def swp_path(name: str) -> Path:
    """Return a path to the cached ``SWP`` file ``name``, fetched + extracted on
    first call. SHA-256 verified. Idempotent and safe under ``pytest-xdist``."""
    if name not in SWP_FILES:
        raise KeyError(f"unknown SWP file {name!r}; expected one of {SWP_NAMES}")
    member, expected_sha = SWP_FILES[name]
    dest = swm_cache_dir() / name
    if dest.is_file() and _sha256(dest) == expected_sha:
        return dest

    tarball = _tarball_path()
    with tarfile.open(tarball, "r:gz") as tf:
        src = tf.extractfile(member)
        if src is None:
            raise RuntimeError(f"tarball member {member!r} is not a regular file")
        data = src.read()

    got = hashlib.sha256(data).hexdigest()
    if got != expected_sha:
        raise RuntimeError(f"{name} SHA-256 mismatch: got {got}, expected {expected_sha}")
    _atomic_write(dest, data)
    return dest


def quickscore_path() -> Path:
    """SHA-256 verified path to quickscore.swm from the SID-Wizard 1.7 smallpack."""
    dest = swm_cache_dir() / "quickscore.swm"
    if dest.is_file() and _sha256(dest) == QUICKSCORE_SHA256:
        return dest
    archive = swm_cache_dir() / "SID-Wizard-1.7-smallpack.zip"
    if not (archive.is_file() and _sha256(archive) == SW17_ZIP_SHA256):
        data = _download(SW17_ZIP_URL)
        got = hashlib.sha256(data).hexdigest()
        if got != SW17_ZIP_SHA256:
            raise RuntimeError(
                f"SID-Wizard-1.7-smallpack.zip SHA-256 mismatch: got {got}, expected {SW17_ZIP_SHA256}"
            )
        _atomic_write(archive, data)
    with zipfile.ZipFile(archive) as zf:
        data = zf.read(QUICKSCORE_MEMBER)
    got = hashlib.sha256(data).hexdigest()
    if got != QUICKSCORE_SHA256:
        raise RuntimeError(
            f"quickscore.swm SHA-256 mismatch: got {got}, expected {QUICKSCORE_SHA256}"
        )
    _atomic_write(dest, data)
    return dest
