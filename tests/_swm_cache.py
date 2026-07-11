"""Fetch + cache the four SWM test tunes on demand.

The repo deliberately does NOT track ``tests/data/*.swm`` — they're
SID-Wizard binary artifacts, redistributed as part of the SID-Wizard
1.94 source tarball. This module downloads that tarball directly from
CSDB (SHA-256 verified) and extracts only the four tunes the player
test suite uses.

Each tune's bytes are verified by SHA-256 against the value bundled in
the SID-Wizard 1.94 release. A mismatch is a hard error — never let a
test run against an unexpected SWM.

Usage::

    from tests._swm_cache import swm_path
    p = swm_path("flashitback")  # Path to a verified, cached SWM.

Test-only module — imported only from ``tests/``.
"""

from __future__ import annotations

import hashlib
import os
import tarfile
import time
import urllib.error
import urllib.request
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
