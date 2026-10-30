"""Synthetic modules played through SID-Maker's export: the playspec and the 6502 it states."""

from __future__ import annotations

from typing import Dict, List

from pysidtracker import SidImage, parse_sid_header
from pysidtracker.oracle import EmuPlayer

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

STACK = range(0x100, 0x200)
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


def note_word(player: SWMPlayer, note: int) -> int:
    """The FREQTBL/FREQTBH word the player's own image holds for 0-based table index ``note``."""
    lab, mem = labels(player), player.memory
    return mem[lab["freqtbl"] + note] | mem[lab["freqtbh"] + note] << 8


def assert_6502(swm: SWMFile, frames: int) -> SWMPlayer:
    """Every call leaves the SID and the memory outside the stack page as the 6502 does."""
    spec = SWMPlayer(swm)
    cpu = emu(spec)
    for frame in range(frames):
        assert spec.play_frame() == cpu.play_frame(), f"call {frame} SID writes"
        mine, real = spec.memory, bytes(cpu._mem)  # pylint: disable=protected-access
        diff = [a for a in range(0x10000) if mine[a] != real[a] and a not in STACK]
        assert not diff, f"call {frame} memory differs at {[f'${a:04x}' for a in diff[:8]]}"
    return spec
