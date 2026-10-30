"""SWM1 first-frame waveform: a stored $00 plays as the editor's $09 default.

The editor's depacker substitutes $09 for a zero first-waveform byte on load
(``packdepack.inc:693-699``), and its ``.sid``/``.prg`` exports carry $09.
The model keeps the raw stored byte so ``build_swm`` round-trips exactly.
"""

from __future__ import annotations

import pytest

from pysidwizard import (
    End,
    Instrument,
    Pattern,
    PlayPattern,
    Row,
    SWMFile,
    SWMPlayer,
    build_swm,
    parse_swm,
    straight_tempo,
)
from tests._swm_cache import quickscore_path
from tests._toy import SETUP_FRAMES, assert_6502, labels

FIRST_WAVEFORM = 0x0F


def _first_waveforms(player: SWMPlayer, count: int) -> list:
    """The first-waveform byte of instruments 1..``count`` in the played image."""
    lab, mem = labels(player), player.memory
    ptrs = (mem[lab["p_insl1"] + k] | mem[lab["p_insh1"] + k] << 8 for k in range(1, count + 1))
    return [mem[ptr + FIRST_WAVEFORM] for ptr in ptrs]


def test_effective_first_waveform_substitutes_swm1_default():
    """Only a stored $00 becomes $09."""
    assert Instrument(first_waveform=0).effective_first_waveform == 0x09
    assert Instrument(first_waveform=0x11).effective_first_waveform == 0x11
    assert Instrument(first_waveform=0x40).effective_first_waveform == 0x40
    assert Instrument().effective_first_waveform == 0x09


def test_effective_first_waveform_keeps_raw_field():
    """The raw byte stays in the model."""
    ins = Instrument(name=b"QS      ", control=0x1A, first_waveform=0)
    assert ins.first_waveform == 0
    assert ins.effective_first_waveform == 0x09


@pytest.mark.parametrize("stored, played", [(0x00, 0x09), (0x40, 0x40)])
def test_first_frame_control_is_the_effective_first_waveform(stored, played):
    """The note's first frame writes CTRL = effective first waveform; the raw byte round-trips."""
    inst = Instrument(
        name=b"QS      ",
        control=0x1A,
        sustain=0xF,
        first_waveform=stored,
        wf_table=bytes([0x41, 0x80, 0x00]),
    )
    pattern = Pattern(rows=[Row(note=49, instrument=1)] + [Row()] * 4, length=5)
    swm = SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pattern],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(3), straight_tempo(3))],
    )
    assert parse_swm(build_swm(swm)).instruments[0].first_waveform == stored
    player = assert_6502(swm, SETUP_FRAMES + 2)
    assert _first_waveforms(player, 1) == [played]
    assert SWMPlayer(swm).render_grid(SETUP_FRAMES + 1)[SETUP_FRAMES][4] == played


def test_quickscore_native_render_uses_editor_first_waveform():
    """quickscore.swm stores $00 in all nine first-waveform bytes with HR enabled;
    its export carries $09 and the voices' first frames write it."""
    data = quickscore_path().read_bytes()
    swm = parse_swm(data)
    assert len(swm.instruments) == 9
    assert all(ins.first_waveform == 0 and ins.control & 0x08 for ins in swm.instruments)
    assert build_swm(swm) == data

    player = assert_6502(swm, 50)
    assert _first_waveforms(player, 9) == [0x09] * 9
    ctrl = {v for _ in range(300) for r, v in player.play_frame() if r in (4, 11, 18)}
    assert 0x09 in ctrl and 0x00 not in ctrl
