"""Writer for SID-Wizard SWM module files."""

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
    PATTERN_AMOUNT_POS,
    RESERVED_POS,
    SEQUENCE_AMOUNT_POS,
    SWM_MAGIC,
    TEMPO_LENGTH_POS,
    TUNE_HEADER_SIZE,
    TUNING_TYPE_POS,
)
from .errors import SWMFormatError
from .model import (
    End,
    Loop,
    PlayPattern,
    SubtuneJump,
    SWMFile,
    encode_sequence,
    pack_pattern,
)


def _byte(value: int, field_name: str) -> int:
    if not (0 <= value <= 0xFF):
        raise SWMFormatError(f"{field_name}={value} does not fit in an unsigned byte")
    return value


def _pad_fixed(buf: bytes, size: int, field_name: str) -> bytes:
    if len(buf) > size:
        raise SWMFormatError(f"{field_name} is {len(buf)} bytes; max {size}")
    return buf.ljust(size, b"\x00")


def _validate_cross_refs(swm: SWMFile) -> None:
    """Catch obvious inter-component mistakes before we start writing."""
    pattern_count = len(swm.patterns)
    instrument_count = len(swm.instruments)
    for s_idx, commands in enumerate(swm.sequences):
        for c_idx, cmd in enumerate(commands):
            if isinstance(cmd, PlayPattern):
                # 0 is the reserved empty-pattern slot; user patterns are
                # numbered 1..len(patterns).
                if cmd.pattern == 0:
                    continue
                if not (1 <= cmd.pattern <= pattern_count):
                    raise SWMFormatError(
                        f"sequences[{s_idx}][{c_idx}] references pattern "
                        f"{cmd.pattern}, but only {pattern_count} patterns "
                        "are defined"
                    )
        if not any(isinstance(c, (End, Loop, SubtuneJump)) for c in commands):
            raise SWMFormatError(
                f"sequences[{s_idx}] does not contain an End, Loop or SubtuneJump "
                "terminator; the C64 player would walk off the end"
            )
    for p_idx, ptn in enumerate(swm.patterns):
        for r_idx, row in enumerate(ptn.rows):
            inst = row.instrument
            # Instrument-column FX (0x3F..0x7F) live in the same column
            # as instrument selections, so don't treat them as references.
            if inst is None or inst >= 0x3F:
                continue
            if not (1 <= inst <= instrument_count):
                raise SWMFormatError(
                    f"patterns[{p_idx}].rows[{r_idx}] uses instrument "
                    f"{inst}, but only {instrument_count} instruments are "
                    "defined"
                )


def build_swm(swm: SWMFile) -> bytes:
    """Serialise a :class:`SWMFile` to a bytes object.

    If ``swm.load_address`` is not ``None`` the result is prefixed with
    that address as a little-endian word, producing a ``.prg``-loadable
    file (the form SID-Wizard itself emits). Set it to ``None`` to get
    the bare module payload without a PRG wrapper.
    """

    _validate_cross_refs(swm)
    out = bytearray()

    if swm.load_address is not None:
        if not (0 <= swm.load_address <= 0xFFFF):
            raise SWMFormatError(f"load_address={swm.load_address} does not fit in a word")
        out += swm.load_address.to_bytes(2, "little")

    sequence_amount = len(swm.sequences)
    pattern_amount = len(swm.patterns)
    instrument_amount = len(swm.instruments)
    chord_length = len(swm.chord_table)
    tempo_length = len(swm.tempo_table)

    for amount, label in (
        (sequence_amount, "sequences"),
        (pattern_amount, "patterns"),
        (instrument_amount, "instruments"),
        (chord_length, "chord_table"),
        (tempo_length, "tempo_table"),
    ):
        if amount > 0xFF:
            raise SWMFormatError(f"{label} has {amount} entries; max 255 fit in the SWM header")

    if len(swm.mute) != 3:
        raise SWMFormatError(f"mute must be 3 bytes (got {len(swm.mute)})")
    if len(swm.reserved) != 3:
        raise SWMFormatError(f"reserved must be 3 bytes (got {len(swm.reserved)})")

    header = bytearray(TUNE_HEADER_SIZE)
    header[0:4] = SWM_MAGIC
    header[FRAMESPEED_POS] = _byte(swm.frame_speed, "frame_speed")
    header[HIGHLIGHT_POS] = _byte(swm.highlight, "highlight")
    header[AUTO_POS] = _byte(swm.auto_advance, "auto_advance")
    header[CONFIG_BITS_POS] = _byte(swm.config_bits, "config_bits")
    header[MUTE_POS : MUTE_POS + 3] = swm.mute
    header[DEFAULT_PATTERN_LEN_POS] = _byte(swm.default_pattern_length, "default_pattern_length")
    header[SEQUENCE_AMOUNT_POS] = sequence_amount
    header[PATTERN_AMOUNT_POS] = pattern_amount
    header[INSTRUMENT_AMOUNT_POS] = instrument_amount
    header[CHORD_LENGTH_POS] = chord_length
    header[TEMPO_LENGTH_POS] = tempo_length
    header[COLOR_THEME_POS] = _byte(swm.color_theme, "color_theme")
    header[KEYBOARD_TYPE_POS] = _byte(swm.keyboard_type, "keyboard_type")
    header[DRIVER_TYPE_POS] = _byte(swm.driver_type, "driver_type")
    header[TUNING_TYPE_POS] = _byte(swm.tuning_type, "tuning_type")
    header[RESERVED_POS:AUTHOR_POS] = swm.reserved
    header[AUTHOR_POS : AUTHOR_POS + AUTHOR_LEN] = _pad_fixed(swm.author, AUTHOR_LEN, "author")
    out += bytes(header)

    # Sequences: encode each command list to bytes, then write body
    # followed by a one-byte size field.
    for i, commands in enumerate(swm.sequences):
        seq_bytes = encode_sequence(commands)
        if len(seq_bytes) > 0xFF:
            raise SWMFormatError(
                f"sequence {i} encodes to {len(seq_bytes)} bytes; size " "byte cannot exceed 255"
            )
        out += seq_bytes
        out += bytes([len(seq_bytes)])

    # Patterns: encode rows to the unpacked stream, pack it, then write
    # body + size byte + length byte. The size byte is the *unpacked*
    # length including the implicit 0xFF row-terminator, so it derives
    # from the row stream rather than from the packed bytes.
    for i, ptn in enumerate(swm.patterns):
        unpacked = ptn.encode_unpacked()
        packed = pack_pattern(unpacked)
        if ptn.size_override is not None:
            size_byte = ptn.size_override
        else:
            size_byte = len(unpacked) + 1
        if not (0 <= size_byte <= 0xFF):
            raise SWMFormatError(f"pattern {i} size byte {size_byte} does not fit in a byte")
        length = ptn.effective_length()
        out += packed
        out += bytes([size_byte, _byte(length, f"patterns[{i}].length")])

    # Instruments: encode each one's bytes (header + WF + PW + filter
    # data), then a size byte (= len(encoded body)), then the 8-byte name.
    for i, ins in enumerate(swm.instruments):
        body = ins.encode()
        if len(body) > 0xFF:
            raise SWMFormatError(
                f"instrument {i} body is {len(body)} bytes; size byte " "cannot exceed 255"
            )
        if len(ins.name) > INSTRUMENT_NAME_LEN:
            raise SWMFormatError(
                f"instrument {i} name is {len(ins.name)} bytes; max " f"{INSTRUMENT_NAME_LEN}"
            )
        out += body
        out += bytes([len(body)])
        out += ins.name.ljust(INSTRUMENT_NAME_LEN, b"\x00")

    out += swm.chord_table
    out += swm.tempo_table

    expected_subtunes = swm.subtune_count
    if len(swm.subtune_tempos) != expected_subtunes:
        raise SWMFormatError(
            f"subtune_tempos has {len(swm.subtune_tempos)} entries but the "
            f"sequence layout requires {expected_subtunes}"
        )
    for i, (left, right) in enumerate(swm.subtune_tempos):
        out += bytes(
            [
                _byte(left, f"subtune_tempos[{i}][0]"),
                _byte(right, f"subtune_tempos[{i}][1]"),
            ]
        )

    return bytes(out)


def write_swm(swm: SWMFile, path: Union[str, os.PathLike[str]]) -> None:
    """Serialise ``swm`` and write it to ``path``."""
    payload = build_swm(swm)
    with open(path, "wb") as fh:
        fh.write(payload)
