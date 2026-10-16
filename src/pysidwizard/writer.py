"""Writer for SID-Wizard SWM module files."""

from __future__ import annotations

import os
from typing import Union

from .constants import (
    AMOUNT_FIELDS,
    INSTRUMENT_NAME_LEN,
    SWM_FORMATS,
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


def _validate_cross_refs(swm: SWMFile, strict: bool = False) -> None:
    """Catch inter-component mistakes; with ``strict`` also cap instrument refs at the count."""
    pattern_count = len(swm.patterns)
    for s_idx, commands in enumerate(swm.sequences):
        for c_idx, cmd in enumerate(commands):
            if isinstance(cmd, PlayPattern):
                if cmd.pattern == 0:  # 0 is the reserved empty-pattern placeholder.
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
    if not strict:
        return
    instrument_count = len(swm.instruments)
    for p_idx, ptn in enumerate(swm.patterns):
        for r_idx, row in enumerate(ptn.rows):
            inst = row.instrument
            if inst is None:  # no instrument column.
                continue
            if inst >= 0x3F:  # >= 0x3F is instrument-column FX, not a reference.
                continue
            if not (1 <= inst <= instrument_count):
                raise SWMFormatError(
                    f"patterns[{p_idx}].rows[{r_idx}] uses instrument "
                    f"{inst}, but only {instrument_count} instruments are "
                    "defined"
                )


def build_swm(swm: SWMFile, strict: bool = False) -> bytes:
    """Serialise a :class:`SWMFile` to a bytes object.

    ``strict=True`` rejects instrument-column references past the module's
    instrument count; the default round-trips editor-stored refs verbatim.
    ``load_address`` set prefixes a little-endian word (the ``.prg`` form).
    """

    _validate_cross_refs(swm, strict)

    fmt = SWM_FORMATS.get(swm.sid_amount)
    if fmt is None:
        raise SWMFormatError(f"swm.sid_amount={swm.sid_amount} is not a supported SID count (1..4)")

    out = bytearray()

    if swm.load_address is not None:
        if not (0 <= swm.load_address <= 0xFFFF):
            raise SWMFormatError(f"load_address={swm.load_address} does not fit in a word")
        out += swm.load_address.to_bytes(2, "little")

    amounts = {
        "sequence_amount": len(swm.sequences),
        "pattern_amount": len(swm.patterns),
        "instrument_amount": len(swm.instruments),
        "chord_length": len(swm.chord_table),
        "tempo_length": len(swm.tempo_table),
    }
    amount_labels = {
        "sequence_amount": "sequences",
        "pattern_amount": "patterns",
        "instrument_amount": "instruments",
        "chord_length": "chord_table",
        "tempo_length": "tempo_table",
    }
    for name in sorted(amounts):
        if amounts[name] > 0xFF:
            raise SWMFormatError(
                f"{amount_labels[name]} has {amounts[name]} entries; "
                "max 255 fit in the SWM header"
            )

    if len(swm.mute) != fmt.expected_mute:
        raise SWMFormatError(f"mute must be {fmt.expected_mute} bytes (got {len(swm.mute)})")
    if len(swm.reserved) != fmt.expected_reserved:
        raise SWMFormatError(
            f"reserved must be {fmt.expected_reserved} " f"bytes (got {len(swm.reserved)})"
        )
    if len(swm.sid_addresses) != len(fmt.sid_addresses):
        raise SWMFormatError(
            f"sid_addresses must have {len(fmt.sid_addresses)} entries "
            f"(got {len(swm.sid_addresses)})"
        )

    header = bytearray(fmt.header_size)
    header[0:4] = fmt.magic
    for name, (off, width) in fmt.fields.items():
        if name in AMOUNT_FIELDS:
            header[off] = amounts[name]
        elif name == "mute":
            header[off : off + width] = swm.mute
        elif name == "author":
            header[off : off + width] = _pad_fixed(swm.author, width, "author")
        else:
            header[off] = _byte(getattr(swm, name), name)
    for off, sid_addr in zip(fmt.sid_addresses, swm.sid_addresses, strict=True):
        header[off] = _byte(sid_addr, "sid_address")
    for off, byte in zip(fmt.reserved, swm.reserved, strict=True):
        header[off] = byte
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
