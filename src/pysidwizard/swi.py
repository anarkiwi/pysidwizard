"""Reader and writer for SID-Wizard's single-instrument ``.swi`` files.

A ``.swi`` is the editor's Save/Load Instrument menu output: the SWM instrument
record wrapped in a 2-byte little-endian PRG load address (see
:meth:`Instrument.decode` / :meth:`Instrument.encode`).
"""

from __future__ import annotations

import os
from typing import Union

from .constants import INST_HEADER_SIZE, INSTRUMENT_NAME_LEN
from .errors import SWMFormatError
from .model import Instrument, SWMFile

# ``saveins`` KERNAL-SAVEs into the screen buffer; every release shares this.
SWI_DEFAULT_LOAD_ADDRESS = 0x04A1

SWI_NAME_LEN = INSTRUMENT_NAME_LEN
SWI_SIZE_NAME = 1 + SWI_NAME_LEN
SWI_MIN_SIZE = 2 + INST_HEADER_SIZE + SWI_SIZE_NAME


def _resolve_load_address(instrument: Instrument, explicit: int | None) -> int:
    addr = explicit if explicit is not None else instrument.load_address
    if addr is None:
        addr = SWI_DEFAULT_LOAD_ADDRESS
    if not (0 <= addr <= 0xFFFF):
        raise SWMFormatError(f"load_address={addr} does not fit in a 16-bit word")
    return addr


def parse_swi(data: bytes) -> Instrument:
    """Decode a ``.swi`` byte string into an :class:`Instrument`.

    The 2-byte PRG load address is recovered onto
    :attr:`Instrument.load_address`. Raises :class:`SWMFormatError` on
    truncation or a size byte that disagrees with the instrument body.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise SWMFormatError(f".swi input must be a byte string, not {type(data).__name__}")
    data = bytes(data)
    if len(data) < SWI_MIN_SIZE:
        raise SWMFormatError(
            f".swi is {len(data)} bytes; need at least {SWI_MIN_SIZE} "
            "(load address + 16-byte header + size byte + 8-byte name)"
        )
    name = data[-SWI_NAME_LEN:]
    size_byte = data[-SWI_SIZE_NAME]
    body = data[2:-SWI_SIZE_NAME]
    if len(body) != size_byte:
        raise SWMFormatError(
            f".swi size byte {size_byte} disagrees with the {len(body)}-byte " "instrument body"
        )
    instrument = Instrument.decode(body, name=name)
    instrument.load_address = int.from_bytes(data[0:2], "little")
    return instrument


def build_swi(instrument: Instrument, load_address: int | None = None) -> bytes:
    """Serialise an :class:`Instrument` to a ``.swi`` byte string.

    ``load_address`` overrides :attr:`Instrument.load_address`, which otherwise
    falls back to :data:`SWI_DEFAULT_LOAD_ADDRESS`. Round-trips with
    :func:`parse_swi` byte-exactly.
    """
    body = instrument.encode()
    if len(body) > 0xFF:
        raise SWMFormatError(
            f"instrument body is {len(body)} bytes; the size byte cannot exceed 255"
        )
    name = instrument.name
    if len(name) > SWI_NAME_LEN:
        raise SWMFormatError(f"instrument name is {len(name)} bytes; max {SWI_NAME_LEN}")
    addr = _resolve_load_address(instrument, load_address)
    return addr.to_bytes(2, "little") + body + bytes([len(body)]) + name.ljust(SWI_NAME_LEN, b" ")


def read_swi(path: Union[str, os.PathLike[str]]) -> Instrument:
    """Read a ``.swi`` file from ``path`` into an :class:`Instrument`."""
    with open(path, "rb") as fh:
        return parse_swi(fh.read())


def write_swi(
    instrument: Instrument,
    path: Union[str, os.PathLike[str]],
    load_address: int | None = None,
) -> None:
    """Serialise ``instrument`` and write it to ``path`` as a ``.swi`` file."""
    with open(path, "wb") as fh:
        fh.write(build_swi(instrument, load_address))


def insert_instrument(swm: SWMFile, slot: int, instrument: Instrument) -> SWMFile:
    """Store ``instrument`` at 1-based ``slot`` (the editor's "load into slot N").

    Replaces the named slot when it exists; appends when ``slot`` is the next
    free slot. Returns ``swm``.
    """
    if slot < 1:
        raise SWMFormatError(f"instrument slot {slot} must be >= 1")
    if slot <= len(swm.instruments):
        swm.instruments[slot - 1] = instrument
    elif slot == len(swm.instruments) + 1:
        swm.instruments.append(instrument)
    else:
        raise SWMFormatError(
            f"instrument slot {slot} exceeds the "
            f"{len(swm.instruments)}-slot bank and is not the next free slot"
        )
    return swm


__all__ = [
    "SWI_DEFAULT_LOAD_ADDRESS",
    "SWI_MIN_SIZE",
    "SWI_NAME_LEN",
    "SWI_SIZE_NAME",
    "build_swi",
    "insert_instrument",
    "parse_swi",
    "read_swi",
    "write_swi",
]
