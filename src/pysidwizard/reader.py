"""Reader for SID-Wizard SWM module files."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Union

from pysidtracker.facets import inferred

from .constants import (
    INSTRUMENT_NAME_LEN,
    P00_HEADER_SIZE,
    P00_MAGIC,
    P00_NAME_LEN,
    P00_NAME_OFFSET,
    PACKED_MAX,
    PACKED_MIN,
    SWM_MAGIC_TO_FORMAT,
    SWMFormat,
    subtunes_for,
)
from .errors import SWMFormatError
from .features import features_for_driver
from .grammar import GRAMMAR, parse_instrument, parse_pattern, parse_sequence
from .model import Instrument, Pattern, SWMFile
from .player_images import LATEST_PLAYER_VERSION, image_for
from .variant import PlayerImage


def apply_player_image(swm: SWMFile, player_version: str | None) -> SWMFile:
    """Fill a mono module's FREQTBL image from the named release's build.

    A ``.swm`` has no player image, so past-``FREQTBL`` lookups would
    otherwise read a fallback; this sets :attr:`SWMFile.player`'s tables and
    first-frame quirk from the shipped image for the driver (no-op if unshipped
    or for multi-SID files).
    """
    player = swm.player
    if (
        swm.sid_amount != 1
        or player_version is None
        or (player and player.freq_lo and player.freq_hi)
    ):
        return swm
    image = image_for(player_version, swm.driver_type)
    if image is None:
        return swm
    lo, hi, by_track = image
    player = player or PlayerImage(features_for_driver(swm.driver_type))
    swm.player = replace(
        player,
        freq_lo=lo,
        freq_hi=hi,
        quirks=replace(player.quirks, first_frame_freq_by_track=by_track),
        provenance={
            **player.provenance,
            "first_frame_freq_by_track": inferred(f"player image {player_version}"),
        },
    )
    return swm


def _detect_layout(data: bytes) -> tuple[int, int | None, SWMFormat, bytes | None]:
    """Return ``(header_offset, load_address, format, p00_name)`` for SWM bytes.

    Accepts a bare module (magic at offset 0), a PRG-wrapped module (2-byte
    load address, magic at offset 2), or a PC64 ``.P00`` file (``C64File``
    header, magic at 0x1C, ``p00_name`` set; ``None`` otherwise).
    """
    if len(data) >= 8 and data[:8] == P00_MAGIC:
        magic_pos = P00_HEADER_SIZE + 2
        if len(data) < magic_pos + 4:
            raise SWMFormatError(
                "input looks like a PC64 .P00 file but is too short to hold a SWM module"
            )
        fmt = SWM_MAGIC_TO_FORMAT.get(data[magic_pos : magic_pos + 4])
        if fmt is None:
            raise SWMFormatError(
                "PC64 .P00 header present but no SWM1/SWMS/SWT/SWMQ magic at offset 0x1C"
            )
        load_address = int.from_bytes(data[P00_HEADER_SIZE : P00_HEADER_SIZE + 2], "little")
        return (
            magic_pos,
            load_address,
            fmt,
            data[P00_NAME_OFFSET : P00_NAME_OFFSET + P00_NAME_LEN],
        )
    if len(data) >= 4:
        fmt = SWM_MAGIC_TO_FORMAT.get(data[0:4])
        if fmt is not None:
            return 0, None, fmt, None
    if len(data) >= 6:
        fmt = SWM_MAGIC_TO_FORMAT.get(data[2:6])
        if fmt is not None:
            return 2, int.from_bytes(data[0:2], "little"), fmt, None
    raise SWMFormatError(
        "input does not look like an SWM module (expected SWM1/SWMS/SWT/SWMQ at "
        "offset 0, 2, or a PC64 .P00 header)"
    )


def parse_swm(data: bytes, player_version: str | None = LATEST_PLAYER_VERSION) -> SWMFile:
    """Parse a SWM module and return a :class:`SWMFile`.

    ``player_version`` selects the release whose player image a bare
    (image-less) module plays with (see :mod:`pysidwizard.player_images`); it
    is ignored by an image that a ``.sid`` already carries.
    """

    header_offset, load_address, fmt, p00_name = _detect_layout(data)
    if len(data) - header_offset < fmt.header_size:
        raise SWMFormatError(
            f"input is shorter than the {fmt.header_size}-byte {fmt.magic.decode()} header"
        )

    header = data[header_offset : header_offset + fmt.header_size]

    sequence_amount = fmt.field(header, "sequence_amount")
    pattern_amount = fmt.field(header, "pattern_amount")
    instrument_amount = fmt.field(header, "instrument_amount")
    chord_length = fmt.field(header, "chord_length")
    tempo_length = fmt.field(header, "tempo_length")

    body_start = header_offset + fmt.header_size
    cursor = len(data)
    subtune_count = subtunes_for(sequence_amount, fmt.channels)

    # 1. Subtune tempos at the very end (each is a (left, right) pair, stored
    #    on disk in that order — i.e. left byte first, right byte second).
    if cursor - 2 * subtune_count < body_start:
        raise SWMFormatError("not enough data for subtune-tempo block")
    subtune_tempos: list[tuple[int, int]] = []
    cursor -= 2 * subtune_count
    for i in range(subtune_count):
        left = data[cursor + 2 * i]
        right = data[cursor + 2 * i + 1]
        subtune_tempos.append((left, right))

    # 2. Tempo table (raw bytes).
    if cursor - tempo_length < body_start:
        raise SWMFormatError("not enough data for tempo table")
    cursor -= tempo_length
    tempo_table = bytes(data[cursor : cursor + tempo_length])
    GRAMMAR.parse_exact(tempo_table, "tempos")

    # 3. Chord table (raw bytes).
    if cursor - chord_length < body_start:
        raise SWMFormatError("not enough data for chord table")
    cursor -= chord_length
    chord_table = bytes(data[cursor : cursor + chord_length])
    GRAMMAR.parse_exact(chord_table, "chords")

    # 4. Instruments (backward): for each, 8-byte name preceded by a 1-byte
    #    size field preceded by `size` bytes of instrument data. The bytes
    #    are then parsed by the ``instrument`` rule of the song grammar into
    #    named fields and variable-length tables.
    instruments: list[Instrument] = []
    for _ in range(instrument_amount):
        if cursor - INSTRUMENT_NAME_LEN < body_start:
            raise SWMFormatError("not enough data for instrument name")
        cursor -= INSTRUMENT_NAME_LEN
        name = bytes(data[cursor : cursor + INSTRUMENT_NAME_LEN])
        if cursor - 1 < body_start:
            raise SWMFormatError("not enough data for instrument size byte")
        cursor -= 1
        size = data[cursor]
        if cursor - size < body_start:
            raise SWMFormatError("not enough data for instrument body")
        cursor -= size
        instr_data = bytes(data[cursor : cursor + size])
        instruments.append(parse_instrument(instr_data, name, exact=True))
    instruments.reverse()

    # 5. Patterns (backward): for each, 1-byte length, 1-byte size, then a
    #    NOP-packed body. The size byte holds the *unpacked* pattern length
    #    (including the implicit 0xFF row-terminator), so we walk the body
    #    backwards while undoing the NOP packing only to find where this
    #    pattern starts in the file. Once we have the body slice we
    #    unpack it and parse it into :class:`Row` objects.
    patterns: list[Pattern] = []
    for _ in range(pattern_amount):
        if cursor - 2 < body_start:
            raise SWMFormatError("not enough data for pattern header bytes")
        cursor -= 1
        length = data[cursor]
        cursor -= 1
        size = data[cursor]
        # SID-Wizard's writer always emits a 1-byte 0xFF terminator (size>=1)
        # but some editor-saved files appear to use size==0 for unused slots.
        if size == 0:
            patterns.append(Pattern(rows=[], length=length, size_override=0))
            continue
        body_end = cursor  # one past the last body byte
        # Walk the body backwards, exactly mirroring the decoder loop in
        # SWMconvert.c: each iteration consumes one source byte, and j is
        # decremented by (run + 1) for a packed marker or 1 for a literal,
        # where the extra ``-1`` mimics the outer ``for j--`` decrement.
        j = size - 2
        while j >= 0:
            if cursor - 1 < body_start:
                raise SWMFormatError("not enough data for pattern body")
            cursor -= 1
            byte = data[cursor]
            prev = data[cursor - 1] if cursor - 1 >= body_start else 0
            is_packed = PACKED_MIN <= byte <= PACKED_MAX and (
                prev == 0 or PACKED_MIN <= prev <= PACKED_MAX
            )
            if is_packed:
                j -= byte - PACKED_MIN + 1
            j -= 1
        patterns.append(parse_pattern(bytes(data[cursor:body_end]), length))
    patterns.reverse()

    # 6. Sequences (backward): for each, 1-byte size preceded by `size`
    #    bytes of orderlist commands.
    sequences: list[list] = []
    for _ in range(sequence_amount):
        if cursor - 1 < body_start:
            raise SWMFormatError("not enough data for sequence size byte")
        cursor -= 1
        size = data[cursor]
        if cursor - size < body_start:
            raise SWMFormatError("not enough data for sequence body")
        cursor -= size
        seq_bytes = bytes(data[cursor : cursor + size])
        sequences.append(parse_sequence(seq_bytes))
    sequences.reverse()

    if cursor != body_start:
        raise SWMFormatError(
            f"trailing/leading data mismatch: expected to consume up to "
            f"offset {body_start} but stopped at {cursor}"
        )

    swm = SWMFile(
        **fmt.header_meta(header),
        sequences=sequences,
        patterns=patterns,
        instruments=instruments,
        chord_table=chord_table,
        tempo_table=tempo_table,
        subtune_tempos=subtune_tempos,
        load_address=load_address,
        p00_name=p00_name or b"",
    )
    return apply_player_image(swm, player_version)


def read_swm(
    path: Union[str, os.PathLike[str]], player_version: str | None = LATEST_PLAYER_VERSION
) -> SWMFile:
    """Read a SWM module from a filesystem path into a :class:`SWMFile`."""
    with open(path, "rb") as fh:
        return parse_swm(fh.read(), player_version)
