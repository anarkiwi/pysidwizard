"""Reader for SID-Wizard SWM module files."""

from __future__ import annotations

import os
from typing import Union

from .constants import (
    AUTHOR_LEN,
    AUTHOR_POS,
    AUTO_POS,
    CHORD_LENGTH_POS,
    COLOR_THEME_POS,
    CONFIG_BITS_POS,
    DEFAULT_PATTERN_LEN_POS,
    DRIVER_TYPE_POS,
    FRAMESPEED_POS,
    HIGHLIGHT_POS,
    INSTRUMENT_AMOUNT_POS,
    INSTRUMENT_NAME_LEN,
    KEYBOARD_TYPE_POS,
    MUTE_POS,
    PACKED_MAX,
    PACKED_MIN,
    PATTERN_AMOUNT_POS,
    RESERVED_POS,
    SEQUENCE_AMOUNT_POS,
    SWM_MAGIC,
    TEMPO_LENGTH_POS,
    TUNE_HEADER_SIZE,
    TUNING_TYPE_POS,
)
from .errors import SWMFormatError
from .model import Instrument, Pattern, SWMFile, decode_sequence, unpack_pattern


def _detect_start(data: bytes) -> tuple[int, int | None]:
    """Return ``(header_offset, load_address)`` for the given SWM bytes.

    Accepts both bare SWM files (``b"SWM1"`` at offset 0) and PRG-wrapped
    files (2-byte little-endian load address followed by ``b"SWM1"`` at
    offset 2). Anything else raises :class:`SWMFormatError`.
    """
    if len(data) >= 4 and data[0:4] == SWM_MAGIC:
        return 0, None
    if len(data) >= 6 and data[2:6] == SWM_MAGIC:
        return 2, int.from_bytes(data[0:2], "little")
    raise SWMFormatError(
        "input does not look like a SWM module (missing 'SWM1' magic at " "offset 0 or 2)"
    )


def _compute_subtune_count(sequence_amount: int) -> int:
    """Number of subtunes implied by ``sequence_amount`` (mono SWM only)."""
    if sequence_amount <= 0:
        return 1
    return (sequence_amount - 1) // 3 + 1


def parse_swm(data: bytes) -> SWMFile:
    """Parse a SWM module from a bytes object and return a :class:`SWMFile`."""

    header_offset, load_address = _detect_start(data)
    if len(data) - header_offset < TUNE_HEADER_SIZE:
        raise SWMFormatError("input is shorter than the 64-byte SWM header")

    header = data[header_offset : header_offset + TUNE_HEADER_SIZE]
    frame_speed = header[FRAMESPEED_POS]
    highlight = header[HIGHLIGHT_POS]
    auto_advance = header[AUTO_POS]
    config_bits = header[CONFIG_BITS_POS]
    mute = bytes(header[MUTE_POS : MUTE_POS + 3])
    default_pattern_length = header[DEFAULT_PATTERN_LEN_POS]
    sequence_amount = header[SEQUENCE_AMOUNT_POS]
    pattern_amount = header[PATTERN_AMOUNT_POS]
    instrument_amount = header[INSTRUMENT_AMOUNT_POS]
    chord_length = header[CHORD_LENGTH_POS]
    tempo_length = header[TEMPO_LENGTH_POS]
    color_theme = header[COLOR_THEME_POS]
    keyboard_type = header[KEYBOARD_TYPE_POS]
    driver_type = header[DRIVER_TYPE_POS]
    tuning_type = header[TUNING_TYPE_POS]
    reserved = bytes(header[RESERVED_POS:AUTHOR_POS])
    author = bytes(header[AUTHOR_POS : AUTHOR_POS + AUTHOR_LEN])

    body_start = header_offset + TUNE_HEADER_SIZE
    cursor = len(data)
    subtune_count = _compute_subtune_count(sequence_amount)

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

    # 3. Chord table (raw bytes).
    if cursor - chord_length < body_start:
        raise SWMFormatError("not enough data for chord table")
    cursor -= chord_length
    chord_table = bytes(data[cursor : cursor + chord_length])

    # 4. Instruments (backward): for each, 8-byte name preceded by a 1-byte
    #    size field preceded by `size` bytes of instrument data. The bytes
    #    are then handed to :meth:`Instrument.decode` which parses them
    #    into named fields and variable-length tables.
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
        instruments.append(Instrument.decode(instr_data, name=name))
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
        ptn_packed = bytes(data[cursor:body_end])
        ptn_unpacked = unpack_pattern(ptn_packed)
        patterns.append(Pattern.decode(ptn_unpacked, length=length))
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
        sequences.append(decode_sequence(seq_bytes))
    sequences.reverse()

    if cursor != body_start:
        raise SWMFormatError(
            f"trailing/leading data mismatch: expected to consume up to "
            f"offset {body_start} but stopped at {cursor}"
        )

    return SWMFile(
        frame_speed=frame_speed,
        highlight=highlight,
        auto_advance=auto_advance,
        config_bits=config_bits,
        mute=mute,
        default_pattern_length=default_pattern_length,
        color_theme=color_theme,
        keyboard_type=keyboard_type,
        driver_type=driver_type,
        tuning_type=tuning_type,
        reserved=reserved,
        author=author,
        sequences=sequences,
        patterns=patterns,
        instruments=instruments,
        chord_table=chord_table,
        tempo_table=tempo_table,
        subtune_tempos=subtune_tempos,
        load_address=load_address,
    )


def read_swm(path: Union[str, os.PathLike[str]]) -> SWMFile:
    """Read a SWM module from a filesystem path and return a :class:`SWMFile`."""
    with open(path, "rb") as fh:
        return parse_swm(fh.read())
