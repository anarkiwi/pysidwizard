"""Fetch + cache the four SWM test tunes on demand.

The repo deliberately does NOT track ``tests/data/*.swm`` — they're
SID-Wizard binary artifacts, redistributed as part of the SID-Wizard
1.94 source tarball. This module reuses ``sidwizard-driver``'s tarball
cache (the same one ``fetch_disk1_d64`` populates) and extracts only
the four tunes the player test suite uses.

Each tune's bytes are verified by SHA-256 against the value bundled in
the SID-Wizard 1.94 release. A mismatch is a hard error — never let a
test run against an unexpected SWM.

Usage::

    from tests._swm_cache import swm_path
    p = swm_path("flashitback")  # Path to a verified, cached SWM.

Test-only module — imported only from ``tests/`` and ``tools/``. The
``pysidwizard`` runtime package has no dependency on
``sidwizard-driver``.
"""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path
from typing import Dict, Tuple

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


def _tarball_path() -> Path:
    """Ensure SID-Wizard's source tarball is downloaded and verified,
    then return its filesystem path. Delegates the actual download +
    verification to ``sidwizard-driver`` so we don't duplicate the
    URL / SHA-256 ratchet."""
    # Imported lazily so collecting the test suite doesn't require
    # ``sidwizard-driver`` to be installed when none of the player tests
    # actually run.
    from sidwizard_driver.fetch import default_cache_dir, fetch_disk1_d64

    # ``fetch_disk1_d64`` triggers the download + SHA-256 check of the
    # SID-Wizard-1.94-with-sources.tar.gz file; we don't need the d64
    # itself here, just the side effect of having the tarball cached.
    fetch_disk1_d64()
    return default_cache_dir() / "SID-Wizard-1.94-with-sources.tar.gz"


def swm_cache_dir() -> Path:
    """Local cache directory for extracted SWM bytes."""
    from sidwizard_driver.fetch import default_cache_dir

    out = default_cache_dir() / "swm"
    out.mkdir(parents=True, exist_ok=True)
    return out


def swm_path(tune: str) -> Path:
    """Return a filesystem path to ``{tune}.swm``, fetching + extracting
    on first call. SHA-256 verified against the expected SID-Wizard 1.94
    bytes. Idempotent."""
    if tune not in TUNE_MEMBERS:
        raise KeyError(f"unknown tune {tune!r}; expected one of {TUNE_NAMES}")
    member, expected_sha = TUNE_MEMBERS[tune]
    dest = swm_cache_dir() / f"{tune}.swm"
    if dest.is_file() and _sha256(dest) == expected_sha:
        return dest

    tarball = _tarball_path()
    with tarfile.open(tarball, "r:gz") as tf:
        src = tf.extractfile(member)
        if src is None:
            raise RuntimeError(f"tarball member {member!r} is not a regular file")
        dest.write_bytes(src.read())

    got = _sha256(dest)
    if got != expected_sha:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"{tune}.swm SHA-256 mismatch: got {got}, expected {expected_sha}")
    return dest


def all_swm_paths() -> Dict[str, Path]:
    """Materialise all four tunes; return ``{tune: path}``."""
    return {tune: swm_path(tune) for tune in TUNE_NAMES}
