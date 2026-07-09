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

from dataclasses import dataclass

from pysidtracker import SidFormatError, parse_sid_header

from .constants import SWM_MAGIC
from .errors import SIDFormatError

PSID_MAGIC = b"PSID"
RSID_MAGIC = b"RSID"

# SID-Wizard's "packed" (SWP) export keeps an ``SWP1`` magic instead of (or in
# addition to) the ``SWM1`` tune header. Some packed tunes carry no ``SWM1``
# header at all, so the predicate below accepts either magic.
_SWP_MAGIC = b"SWP1"


@dataclass
class PsidHeader:
    """Decoded SID-container header fields.

    Attributes mirror the on-disk header. :attr:`load_address` is the raw
    header field (``0`` for the common "load address lives in the data" case);
    :attr:`real_load_address` is the resolved C64 address the memory image
    loads at, and :attr:`data_start` is the file offset of the first byte of
    that memory image (i.e. ``data_offset`` plus the optional 2-byte embedded
    load address).
    """

    magic: bytes
    version: int
    data_offset: int
    load_address: int
    init_address: int
    play_address: int
    songs: int
    start_song: int
    flags: int
    second_sid: int
    third_sid: int
    real_load_address: int
    data_start: int

    @property
    def is_psid(self) -> bool:
        return self.magic == PSID_MAGIC

    @property
    def is_rsid(self) -> bool:
        return self.magic == RSID_MAGIC

    @property
    def is_multi_sid(self) -> bool:
        """True if the header advertises a 2nd/3rd/4th SID chip."""
        return self.version >= 3 and (self.second_sid != 0 or self.third_sid != 0)


def parse_psid_header(data: bytes) -> PsidHeader:
    """Decode the SID-container header at the start of ``data``.

    Raises :class:`SIDFormatError` if the magic is neither ``PSID`` nor
    ``RSID`` or the header is truncated. The embedded load address (used when
    the header ``loadAddress`` field is ``0``) is read from the data area.

    Delegates the byte-level decode (including the load-address-0 handling that
    resolves :attr:`PsidHeader.real_load_address` / :attr:`PsidHeader.data_start`)
    to :func:`pysidtracker.parse_sid_header`, re-raising its
    :class:`pysidtracker.SidFormatError` as this package's :class:`SIDFormatError`.
    """
    try:
        header = parse_sid_header(data)
    except SidFormatError as exc:
        raise SIDFormatError(str(exc)) from exc
    return PsidHeader(
        magic=header.magic,
        version=header.version,
        data_offset=header.data_offset,
        load_address=header.load_address,
        init_address=header.init_address,
        play_address=header.play_address,
        songs=header.songs,
        start_song=header.start_song,
        flags=header.flags,
        second_sid=header.second_sid,
        third_sid=header.third_sid,
        real_load_address=header.real_load_address,
        data_start=header.data_start,
    )


def is_sidwizard_sid(data: bytes) -> bool:
    """Return True if ``data`` looks like a single-SID SID-Wizard tune.

    Cheap predicate: a ``PSID`` or ``RSID`` magic, single-SID (version below 3
    with no advertised second/third SID), and an ``SWM1`` tune-header magic (or
    an ``SWP1`` packed-export magic) somewhere in the memory image. Both
    container types carry the same SID-Wizard tune-data layout, so both are
    accepted. Does not attempt a full parse.
    """
    try:
        header = parse_psid_header(data)
    except SIDFormatError:
        return False
    if not (header.is_psid or header.is_rsid):
        return False
    if header.version >= 3 or header.is_multi_sid:
        return False
    return (
        data.find(SWM_MAGIC, header.data_start) >= 0
        or data.find(_SWP_MAGIC, header.data_start) >= 0
    )
