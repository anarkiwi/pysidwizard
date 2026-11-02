"""Synthetic modules played through SID-Maker's export: the playspec and the 6502 it states."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pysidtracker import SidImage, parse_sid_header
from pysidtracker.oracle import EmuPlayer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from sw_spec_diff import audible  # noqa: E402  # pylint: disable=wrong-import-position

from pysidwizard import (
    End,
    Instrument,
    Pattern,
    PlayPattern,
    Row,
    SWMFile,
    SWMPlayer,
    Waveform,
    playspec,
    straight_tempo,
)

# INITER zeroes SPDCNT: calls 0 and 1 are row 0's TICK_0 and TICK_1, its note sounds on call 2.
SETUP_FRAMES = 2


def toy_swm(tempo: int = 3, driver_type: int = 0) -> SWMFile:
    """One pulse instrument and one five-row pattern on all three voices."""
    inst = Instrument(
        name=b"TEST    ",
        sustain=0xF,
        first_waveform=Waveform.PULSE | 0x01,
        wf_table=bytes([0x41, 0x00, 0xFF]),
        pw_table=b"\x88\x00\x00",
    )
    pattern = Pattern(
        rows=[
            Row(note=49, instrument=1),
            Row(),
            Row(note=0x7E),
            Row(note=53, instrument=1),
            Row(),
        ],
        length=5,
    )
    return SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pattern],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(tempo), straight_tempo(tempo))],
        driver_type=driver_type,
    )


def emu(player: SWMPlayer) -> EmuPlayer:
    """The 6502 of ``player``'s own ``.sid``, booted as ``player`` boots it."""
    header = parse_sid_header(player.sid)
    image = SidImage.from_bytes(player.sid)
    return EmuPlayer(
        image.image,
        image.load,
        header.init_address or image.load,
        header.play_address,
        player.swm.song if player.swm.source else 0,
        sid_model=player._sid_model,  # pylint: disable=protected-access
        ntsc=header.is_ntsc,
    )


def grid(swm: SWMFile, frames: int) -> List[List[int]]:
    """``frames`` forward-filled register rows of ``swm``."""
    return SWMPlayer(swm).render_grid(frames)


def reg(swm: SWMFile, frames: int, register: int) -> List[int]:
    """SID ``register``'s value after each of ``frames`` play calls."""
    return [row[register] for row in grid(swm, frames)]


def labels(player: SWMPlayer) -> Dict[str, int]:
    """The binder's addresses of ``player``'s tables and variables (after its first call)."""
    if player.entry is None:
        player.play_frame()
    return playspec.labels(player.view(), player.entry)


_PARTS = {
    "freqlo": ("freq", 0),
    "freqhi": ("freq", 1),
    "pwlogho": ("pw", 0),
    "pwhigho": ("pw", 1),
    "freqmodl": ("freqmod", 0),
    "freqmodh": ("freqmod", 1),
    "ctflgho": ("cutoff", 0),
    "ctfhgho": ("cutoff", 1),
}


def _name(player: SWMPlayer, name: str, voice: int) -> Tuple[str, Optional[int]]:
    """``(declared state name, byte)`` of the variable ``name`` of ``voice`` (or the shared one);
    ``byte`` is the byte of a word state ``name`` names, else None."""
    if player.entry is None:
        player.play_frame()
    want, byte = _PARTS.get(name, (name, None))
    try:
        player.state(want)
        return want, byte
    except KeyError:
        return f"voice[{voice}].{want}", byte


def variable(player: SWMPlayer, name: str, voice: int = 0) -> int:
    """The player variable ``name`` the spec keeps as state: ``voice``'s own, or the shared one.

    A word variable is read by its byte names (``freqlo``, ``pwhigho``, ``freqmodl``...)."""
    full, byte = _name(player, name, voice)
    value = player.state(full)
    return value if byte is None else value >> 8 * byte & 0xFF


def set_variable(player: SWMPlayer, name: str, value: int, voice: int = 0) -> None:
    """Store ``value`` in the player variable ``name`` (a byte of a word by its byte name)."""
    full, byte = _name(player, name, voice)
    old = player.state(full)
    player.set_state(
        full,
        value if byte is None else old & ~(0xFF << 8 * byte) & 0xFFFF | value << 8 * byte,
    )


def note_word(player: SWMPlayer, note: int) -> int:
    """The FREQTBL/FREQTBH word the player's own image holds for 0-based table index ``note``."""
    lab, mem = labels(player), player.memory
    return mem[lab["freqtbl"] + note] | mem[lab["freqtbh"] + note] << 8


def assert_6502(swm: SWMFile, frames: int) -> SWMPlayer:
    """Every call leaves the SID's registers, and the gate and test edges, as the 6502 does."""
    spec = SWMPlayer(swm)
    cpu = emu(spec)
    mine, real = [0] * 3, [0] * 3
    for frame in range(frames):
        want = audible(cpu.play_frame(), cpu.snapshot(), real)
        have = audible(spec.play_frame(), spec.snapshot(), mine)
        assert have == want, f"call {frame} audible output"
    return spec
