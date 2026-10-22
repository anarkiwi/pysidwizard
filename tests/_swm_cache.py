"""Fetch + cache the SWM test tunes on demand, SHA-256 verified, never committed.

CSDB sources: the SID-Wizard 1.94 source tarball (four player-test tunes, one SWP) and
the SID-Wizard 1.7 smallpack (quickscore.swm). Any SHA-256 mismatch is a hard error.
Each file is one member of a pinned release archive, fetched by the shared
:func:`pysidtracker.testing.fetch_archive_member`. Test-only module, imported only
from ``tests/``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Tuple

from pysidtracker.testing import fetch_archive_member

TARBALL_URL = "https://csdb.dk/getinternalfile.php/276275/SID-Wizard-1.94-with-sources.tar.gz"
TARBALL_SHA256 = "544e36aff3fe14b7e4cf81a04c680a6883191a222754b2f0489e15349a89b559"

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

# issue #53: native multi-SID work files (SWMS/SWT/SWMQ), all in the 1.94 tarball.
MULTI_SID_MEMBERS: Dict[str, Tuple[str, str]] = {
    "childhood.sws": (
        "SID-Wizard-1.94/native/examples/childhood.sws",
        "2316115fb12447dce36fd0dfe5339327149936efdcfa3f453b3a846e65ce067a",
    ),
    "egblues.sws": (
        "SID-Wizard-1.94/native/examples/egblues.sws",
        "a6129d0f0c49e75c94cca5eb12cf3211e97c59e43cc2039f08b28a5aef06c2eb",
    ),
    "chb-stickman.sws": (
        "SID-Wizard-1.94/resources/examples/chb-stickman.sws",
        "17dfcba14fbe5321f155442dde57c61cb12af6b2d79c8f8455ddc3c67dafa06b",
    ),
    "newwave.swt": (
        "SID-Wizard-1.94/native/examples/newwave.swt",
        "be745aa47d80f4c90364ae81cb0a366ea0df6e5eb97c580067dc07d84330020c",
    ),
    "techno.swt": (
        "SID-Wizard-1.94/native/examples/techno.swt",
        "c28bb59c0b72f4a218bb3ab98a6e32f7a38ac3f4fc7b7720adead1bf7f661e00",
    ),
    "thizizda.swt": (
        "SID-Wizard-1.94/native/examples/thizizda.swt",
        "f82eb5e0868c19f0dcc443c8449daf6df262cd577f19eb8e9601d8cacab3544c",
    ),
    "ramos.swt": (
        "SID-Wizard-1.94/resources/examples/ramos.swt",
        "1fe367720d1926020854f16388f354c38c67fcff885634a829330a5bda0d5f73",
    ),
    "4sid-example.swq": (
        "SID-Wizard-1.94/resources/examples/4sid-example.swq",
        "87155adffb1928368718f6878b06bc1b0603ce18a944a1f5bb9df89fd16de2f7",
    ),
}

MULTI_SID_NAMES: Tuple[str, ...] = tuple(MULTI_SID_MEMBERS.keys())

# issue #56: the 1.97 release's native .swm and its .sid export (the per-
# (version, driver) FREQTBL image is derived from the .sid exports).
SW197_TARBALL_URL = (
    "https://csdb.dk/getinternalfile.php/281063/SID-Wizard-1.97-sources-examples.tar.gz"
)
SW197_TARBALL_SHA256 = "aeb265f4a778b2acb5aad13e8e98af882888292f749f29f4d18cbf49850e57bc"
_SW197_ROOT = "SID-Wizard-1.97-sources-examples"

SW197_MEMBERS: Dict[str, Tuple[str, str]] = {
    "bronkosaurus": (
        _SW197_ROOT + "/native/examples/bronkosaurus.swm",
        "96862c8fddf77cf3abbf2ff2010b761bcef4ea0968b5843a9408d1aec99e3591",
    ),
    "bronkosaurus.sid": (
        _SW197_ROOT + "/native/examples/sid-exports/bronkosaurus.sid",
        "0f65d71e4ef81a67f79f0cf808e7600f083cddeaa8c5dbc9ba905ec2c7ae16e6",
    ),
    "glacialblues": (
        _SW197_ROOT + "/native/examples/swm-more/extra/glacialblues.swm",
        "caf55c69b599d92dabea6084ffc9a824456f983c2a8a197e8aa926d169cd1c81",
    ),
    "glacialblues.sid": (
        _SW197_ROOT + "/native/examples/sid-exports/glacialblues.sid",
        "494e3ca6e937c9d1904bcb001cbcbb2d20a84d997679caa4a51dab7abebfb18e",
    ),
}

SW197_NAMES: Tuple[str, ...] = tuple(SW197_MEMBERS.keys())

# issue #61: quickscore .P00 container + .prg twin from the SID-Wizard 1.4 release (csdb 115599).
SW14_ZIP_URL = "https://csdb.dk/getinternalfile.php/113613/SID-Wizard-1.4-with-source.zip"
SW14_ZIP_SHA256 = "6cd2e4c482f872a4e12a5ca2989039c6f3df079cab96a55562276a6973364ecc"
_SW14_ROOT = "SID-Wizard-1.4-with-source"

P00_PAIR_MEMBERS: Dict[str, Tuple[str, str]] = {
    "quickscore.swm.P00": (
        _SW14_ROOT + "/demotunes/quickscore.swm.P00",
        "fc4e260c8ab4666232e08fe97e8f0532eb240126629eaeaf89a27ba811a3fb08",
    ),
    "quickscore.swm.prg": (
        _SW14_ROOT + "/demotunes/quickscore.swm.prg",
        "52cf7c8662aa297e7306b421f4966c5a5bacb79c5dd6dee79dca9a704d2329f0",
    ),
}

P00_PAIR_NAMES: Tuple[str, ...] = tuple(P00_PAIR_MEMBERS.keys())

SW194 = (TARBALL_URL, TARBALL_SHA256)
SW197 = (SW197_TARBALL_URL, SW197_TARBALL_SHA256)
SW17 = (SW17_ZIP_URL, SW17_ZIP_SHA256)
SW14 = (SW14_ZIP_URL, SW14_ZIP_SHA256)


def swm_cache_dir() -> Path:
    """Local cache directory for the release archives and extracted files."""
    env = os.environ.get("SIDWIZARD_TUNECACHE")
    return Path(env) if env else Path(__file__).resolve().parent / ".swmcache"


def release_member(release: Tuple[str, str], member: str, sha256: str, name: str) -> Path:
    """``member`` of the pinned ``(url, sha256)`` ``release`` archive, cached as ``name``."""
    return fetch_archive_member(*release, member, sha256, cache_dir=swm_cache_dir(), name=name)


def swm_path(tune: str) -> Path:
    """Path to the SID-Wizard 1.94 example ``{tune}.swm``."""
    return release_member(SW194, *TUNE_MEMBERS[tune], f"{tune}.swm")


def all_swm_paths() -> Dict[str, Path]:
    """Materialise all four tunes; return ``{tune: path}``."""
    return {tune: swm_path(tune) for tune in TUNE_NAMES}


def oob_inst_path(name: str) -> Path:
    """Issue #57 fixture (instrument ref above the count)."""
    return release_member(SW194, *OOB_INST_TUNE_MEMBERS[name], f"{name}.swm")


def multi_sid_path(name: str) -> Path:
    """Path to a native multi-SID work file (issue #53)."""
    return release_member(SW194, *MULTI_SID_MEMBERS[name], name)


def swp_path(name: str) -> Path:
    """Path to the packed ``SWP`` file ``name``."""
    return release_member(SW194, *SWP_FILES[name], name)


def quickscore_path() -> Path:
    """Path to quickscore.swm from the SID-Wizard 1.7 smallpack."""
    return release_member(SW17, QUICKSCORE_MEMBER, QUICKSCORE_SHA256, "quickscore.swm")


def sw197_path(name: str) -> Path:
    """Path to a 1.97 release member (``.swm`` or ``.sid``)."""
    return release_member(SW197, *SW197_MEMBERS[name], f"sw197-{name}")


def p00_pair() -> Tuple[Path, Path]:
    """Return ``(p00, prg)`` — the 1.4 quickscore module wrapped and bare (issue #61)."""
    return tuple(release_member(SW14, *P00_PAIR_MEMBERS[n], n) for n in P00_PAIR_NAMES)
