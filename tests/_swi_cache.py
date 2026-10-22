"""Curated ``.swi`` fixtures for issue #54: a SHA-256-pinned subset, fetched on demand.

Spread of single-instrument files (incl. the canonical ``agogo.swi`` and a SWM/``.swi``
shared-instrument cross-check pair) extracted from the pinned SID-Wizard 1.97
tarball; any SHA-256 mismatch is a hard error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

from tests._swm_cache import SW197, release_member

_ROOT = "SID-Wizard-1.97-sources-examples"
_INST = f"{_ROOT}/native/examples/instruments"
_DFIT = f"{_INST}/didnt-fit-on-disk"

# fixture id -> (tarball member, expected SHA-256 of the file bytes).
SWI_MEMBERS: Dict[str, Tuple[str, str]] = {
    "agogo": (
        f"{_INST}/agogo.swi",
        "9f0834bc00153de1cae5e9b9b556ff41e5b7ea08fa13bf63ce41a923925975cd",
    ),
    "chord-thinfi": (
        f"{_INST}/chord-thinfi.swi",
        "94a1b109b9bca34fe4f1b08dc628ff8023b07ad57c716fcf7d6c8c13e65fcbec",
    ),
    "cimbalom": (
        f"{_INST}/cimbalom.swi",
        "f57d689e6e6d3edcfdc709586a4da9ecda98c397e23df22493cf678fd6bbb8d0",
    ),
    "organhammond": (
        f"{_INST}/organhammond.swi",
        "11ce98314b71eb2ebbb54df45fad2adf2e4eaf30f97ec3287917084dbfa6ad80",
    ),
    "snare-sidest": (
        f"{_DFIT}/snare-sidest.swi",
        "6d4afd079ade1521e09224b43eb8c8f1741bb0d1fdb6819006e6dbbe9f5096f8",
    ),
    "snare-rota4x": (
        f"{_DFIT}/snare-rota4x.swi",
        "60c138cc2b1dfd9b0ecf613b501195e95e6c8266d34b4a6b7ca6016c512bc2fc",
    ),
    "snare-4x": (
        f"{_DFIT}/snare-4x.swi",
        "7982fc64be804b637a4aa3007f938753cb0d59b9a8fd330393a65ad56ba674c1",
    ),
    "arp-filtupd": (
        f"{_DFIT}/arp-filtupd.swi",
        "25377f229ed97545d23a473d8e060584f0314fcb5e565cba2ef9c421fe750053",
    ),
    "bassassa": (
        f"{_DFIT}/bassassa.swi",
        "59c674101ec2a7ed2a52bd344b91e5f737308fcef9b2d5415863e4a478205b37",
    ),
    "chordfastrng": (
        f"{_DFIT}/chordfastrng.swi",
        "daeb30dc280f26ad7fc0f124e96ff58e8f43296458d862062ab0fc2f334b3a23",
    ),
}

# SWM tune sharing the ``cimbalom.swi`` instrument (cross-format check).
NEPZENEK_MEMBER = (
    f"{_ROOT}/native/examples/nepzenek.swm",
    "62b0d68a982e908d6807d3961e996934a592452d8e604819e3c7a680a99cd250",
)

SWI_NAMES: Tuple[str, ...] = tuple(SWI_MEMBERS.keys())


def swi_path(name: str) -> Path:
    """Return a verified path to the curated ``.swi`` fixture ``name``."""
    return release_member(SW197, *SWI_MEMBERS[name], f"swi-{name}.swi")


def nepzenek_path() -> Path:
    """Verified path to the ``nepzenek.swm`` cross-check fixture (1.97)."""
    return release_member(SW197, *NEPZENEK_MEMBER, "swi-cross-nepzenek.swm")
