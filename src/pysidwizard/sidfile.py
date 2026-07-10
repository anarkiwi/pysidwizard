"""PSID/RSID (``.sid``) container-header parsing.

This module knows only about the outer SID *container* — the big-endian
header documented at https://www.hvsc.c64.org/download/C64Music/DOCUMENTS/SID_file_format.txt
— not about SID-Wizard's tune data. :func:`parse_psid_header` decodes the
header fields and resolves the real C64 load address; :func:`is_sidwizard_sid`
is a cheap predicate used to gate the SID-Wizard reader in
:mod:`pysidwizard.sidreader`.

Only what the Phase-1 reader needs is modelled. Multi-SID detection and the
RSID magic are surfaced so the reader can give clear "not supported yet"
errors rather than mis-parsing.
"""

from __future__ import annotations

from typing import Optional

from pysidtracker import (
    CodePattern,
    SidFormatError,
    SidHeader,
    SidImage,
    find_code_first,
    parse_sid_header,
)

from .constants import SWM_MAGIC
from .errors import SIDFormatError

# The decoded SID-container header is the shared :class:`pysidtracker.SidHeader`
# (``magic`` / ``version`` / ``real_load_address`` / ``data_start`` /
# ``is_psid`` / ``is_rsid`` / ``is_multi_sid`` etc.); re-exported under the
# historical name for callers importing it from this module.
PsidHeader = SidHeader

# SID-Wizard's "packed" (SWP) export keeps an ``SWP1`` magic instead of (or in
# addition to) the ``SWM1`` tune header. Some packed tunes carry no ``SWM1``
# header at all, so the predicate below accepts either magic.
_SWP_MAGIC = b"SWP1"

# SID-Wizard 1.x player-code signature (the fragment ``sidid`` matches for
# ``Hermit/SidWizard_V1.x``). Anchors magic-less exports that carry neither an
# ``SWM1`` nor an ``SWP1`` magic; the player relocates as one block so this
# opcode skeleton is present regardless of load address.
PLAYER_SIGNATURE = CodePattern("F0 04 C0 60 90 03 4C ?? ?? BC")


def find_player_signature(image: SidImage) -> Optional[int]:
    """Return the address of the SID-Wizard 1.x player-code signature, or ``None``."""
    match = find_code_first(image, PLAYER_SIGNATURE)
    return match.addr if match is not None else None


def parse_psid_header(data: bytes) -> SidHeader:
    """Decode the SID-container header at the start of ``data``.

    Thin wrapper over :func:`pysidtracker.parse_sid_header` that re-raises its
    :class:`pysidtracker.SidFormatError` as this package's :class:`SIDFormatError`.
    The returned :class:`pysidtracker.SidHeader` resolves ``real_load_address`` /
    ``data_start`` (handling the ``loadAddress == 0`` embedded-address case).
    """
    try:
        return parse_sid_header(data)
    except SidFormatError as exc:
        raise SIDFormatError(str(exc)) from exc


def is_sidwizard_sid(data: bytes) -> bool:
    """Return True if ``data`` looks like a single-SID SID-Wizard tune.

    Cheap predicate: a ``PSID`` or ``RSID`` magic, single-SID (version below 3
    with no advertised second/third SID), and an ``SWM1`` tune-header magic, an
    ``SWP1`` packed-export magic, or the SID-Wizard 1.x player-code signature
    (magic-less exports) somewhere in the memory image. Both container types
    carry the same SID-Wizard tune-data layout, so both are accepted. Does not
    attempt a full parse.
    """
    try:
        header = parse_psid_header(data)
    except SIDFormatError:
        return False
    if not (header.is_psid or header.is_rsid):
        return False
    if header.version >= 3 or header.is_multi_sid:
        return False
    if (
        data.find(SWM_MAGIC, header.data_start) >= 0
        or data.find(_SWP_MAGIC, header.data_start) >= 0
    ):
        return True
    try:
        return find_player_signature(SidImage.from_sid(data)) is not None
    except (SidFormatError, ValueError):
        return False
