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

from .constants import SWM_MAGIC
from .errors import SIDFormatError

PSID_MAGIC = b"PSID"
RSID_MAGIC = b"RSID"

# Field offsets within the (big-endian) SID container header.
_MAGIC_POS = 0x00
_VERSION_POS = 0x04
_DATA_OFFSET_POS = 0x06
_LOAD_ADDRESS_POS = 0x08
_INIT_ADDRESS_POS = 0x0A
_PLAY_ADDRESS_POS = 0x0C
_SONGS_POS = 0x0E
_START_SONG_POS = 0x10
_FLAGS_POS = 0x76
_SECOND_SID_POS = 0x7A  # v3+: address byte of the 2nd SID (0 => single SID)
_THIRD_SID_POS = 0x7C  # v4+: address byte of the 3rd SID


def _u16be(data: bytes, pos: int) -> int:
    return (data[pos] << 8) | data[pos + 1]


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
    """
    if len(data) < 0x7C:
        raise SIDFormatError("input is too short to contain a SID header")
    magic = bytes(data[_MAGIC_POS : _MAGIC_POS + 4])
    if magic not in (PSID_MAGIC, RSID_MAGIC):
        raise SIDFormatError(
            "not a SID file (expected 'PSID' or 'RSID' magic at offset 0, " f"found {magic!r})"
        )
    version = _u16be(data, _VERSION_POS)
    data_offset = _u16be(data, _DATA_OFFSET_POS)
    load_address = _u16be(data, _LOAD_ADDRESS_POS)
    init_address = _u16be(data, _INIT_ADDRESS_POS)
    play_address = _u16be(data, _PLAY_ADDRESS_POS)
    songs = _u16be(data, _SONGS_POS)
    start_song = _u16be(data, _START_SONG_POS)
    flags = _u16be(data, _FLAGS_POS) if len(data) >= _FLAGS_POS + 2 else 0
    second_sid = data[_SECOND_SID_POS] if len(data) > _SECOND_SID_POS else 0
    third_sid = data[_THIRD_SID_POS] if len(data) > _THIRD_SID_POS else 0

    if data_offset + 2 > len(data):
        raise SIDFormatError("SID dataOffset points past the end of the file")
    if load_address == 0:
        # Real load address is the first little-endian word of the data area.
        real_load = data[data_offset] | (data[data_offset + 1] << 8)
        data_start = data_offset + 2
    else:
        real_load = load_address
        data_start = data_offset

    return PsidHeader(
        magic=magic,
        version=version,
        data_offset=data_offset,
        load_address=load_address,
        init_address=init_address,
        play_address=play_address,
        songs=songs,
        start_song=start_song,
        flags=flags,
        second_sid=second_sid,
        third_sid=third_sid,
        real_load_address=real_load,
        data_start=data_start,
    )


def is_sidwizard_sid(data: bytes) -> bool:
    """Return True if ``data`` looks like a single-SID SID-Wizard tune.

    Cheap predicate: a ``PSID`` or ``RSID`` magic, single-SID (version below 3
    with no advertised second/third SID), and an ``SWM1`` tune-header magic
    somewhere in the memory image. Both container types carry the same
    SID-Wizard tune-data layout, so both are accepted. Does not attempt a full
    parse.
    """
    try:
        header = parse_psid_header(data)
    except SIDFormatError:
        return False
    if not (header.is_psid or header.is_rsid):
        return False
    if header.version >= 3 or header.is_multi_sid:
        return False
    return data.find(SWM_MAGIC, header.data_start) >= 0
