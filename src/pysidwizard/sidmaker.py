"""SID-Maker, SID-Wizard's own exporter, run on the 6502 to turn a ``.swm`` into a ``.sid``.

Each shipped ``data/sidmaker-VERSION.prg`` is that release's single-SID SID-Maker
(``scripts/build_sidmaker.py`` proves it). The export runs SID-Maker's ``preptune``
routines in order with the answers its UI would store: SID output, PAL, default relocation.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Dict, Tuple, Union

from pysidtracker.editor import RAM_SIZE, run_editor
from pysidtracker.image import SidImage

from .errors import SWMFormatError
from .model import SWMFile
from .reader import _detect_layout
from .writer import build_swm

LATEST = "1.97"
MODELS = ("6581", "8580")
_SID_OUTPUT = 3
_PAL = 1
_IO_BANK = 0x35
_SPEED = slice(0x12, 0x16)
_ENTRIES = ("setEXEdetails", "preparetunedata", "disPtyp", "setplayer", "SetSIDply", "SetSIDt")
_DATA = resources.files(__package__) / "data"
_TABLE: Dict[str, Dict[str, int]] = json.loads((_DATA / "sidmaker.json").read_text())
VERSIONS: Tuple[str, ...] = tuple(_TABLE)


@cache
def _image(version: str) -> bytes:
    """64 KiB with SID-Maker ``version`` loaded at its PRG address."""
    prg = (_DATA / f"sidmaker-{version}.prg").read_bytes()
    ram = bytearray(RAM_SIZE)
    load = prg[0] | prg[1] << 8
    ram[load : load + len(prg) - 2] = prg[2:]
    return bytes(ram)


def _module(swm: Union[bytes, SWMFile]) -> bytes:
    """The single-SID module bytes KERNAL ``LOAD`` places at ``MUSICDATA``."""
    data = build_swm(swm) if isinstance(swm, SWMFile) else bytes(swm)
    offset, _load, fmt, _name = _detect_layout(data)
    if len(data) - offset < fmt.header_size:
        raise SWMFormatError(f"module is shorter than its {fmt.header_size}-byte header")
    if fmt.sid_amount != 1:
        raise SWMFormatError(f"{fmt.magic.decode()} is multi-SID; SID-Maker exports SWM1 only")
    return data[offset:]


class SidMaker:  # pylint: disable=too-few-public-methods
    """One SID-Maker session: successive exports share its memory, as on the C64."""

    def __init__(self, version: str = LATEST) -> None:
        if version not in _TABLE:
            raise ValueError(f"no SID-Maker {version!r}; shipped: {', '.join(VERSIONS)}")
        self.version = version
        self.labels = _TABLE[version]
        self.ram = bytearray(_image(version))

    def export(self, swm: Union[bytes, SWMFile], model: str = "8580") -> bytes:
        """The ``.sid`` SID-Maker saves for ``swm`` with the SID ``model`` selected."""
        if model not in MODELS:
            raise ValueError(f"SID model {model!r} is not one of {', '.join(MODELS)}")
        lab, ram = self.labels, self.ram
        module = _module(swm)
        driver = module[lab["DRIVERTYPE_POS"]]
        if driver >= lab["NumberOfPlayerTypes"]:
            raise SWMFormatError(f"driver type {driver} is not a SID-Maker {self.version} player")
        ram[1] = _IO_BANK
        ram[lab["outputformat"]] = _SID_OUTPUT
        ram[lab["PALNTSC"]] = _PAL
        ram[lab["relocaddr"]] = lab["PLAYERADDR"] >> 8
        flags = lab["SIDflags"] + 1
        ram[flags] = ram[flags] & 0xCF | ram[lab["ModelFlag"] + MODELS.index(model)]
        mem = run_editor(
            bytes(ram),
            module,
            lab["MUSICDATA"],
            [lab[name] for name in _ENTRIES],
            load_end=(lab["compzptr"],),
        )
        self.ram = mem
        end = int.from_bytes(mem[lab["expoendadd"] : lab["expoendadd"] + 2], "little")
        head, player = lab["SIDHEADER"], lab["PLAYERADDR"]
        header = bytes(mem[head : head + lab["SIDHEADERSIZE"]])
        if any(header[_SPEED]):
            return bytes(mem[head:end])
        return header + bytes((player & 0xFF, mem[lab["relocaddr"]])) + bytes(mem[player:end])


def export(swm: Union[bytes, SWMFile], version: str = LATEST, model: str = "8580") -> bytes:
    """The ``.sid`` a fresh SID-Maker ``version`` saves for ``swm``."""
    return SidMaker(version).export(swm, model)


def tune(swm: Union[bytes, SWMFile], version: str = LATEST, model: str = "8580") -> SidImage:
    """:func:`export` as a :class:`~pysidtracker.image.SidImage` (64 KiB ``mem``, init, play)."""
    return SidImage.from_sid(export(swm, version, model))


__all__ = ["LATEST", "MODELS", "VERSIONS", "SidMaker", "export", "tune"]
