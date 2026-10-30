"""SWM1 first-frame waveform: a stored $00 plays as the editor's $09 default.

The editor's depacker substitutes $09 for a zero first-waveform byte on load
(``packdepack.inc:693-699``), and its ``.sid``/``.prg`` exports carry $09.

The model keeps the raw stored byte so ``build_swm`` round-trips exactly; the player applies the default.
"""

from __future__ import annotations

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

SETUP_FRAMES = (
    2  # INITER zeroes SPDCNT: row 0's TICK_0/TICK_1 apply on calls 0/1, its note on call 2
)


def test_effective_first_waveform_substitutes_swm1_default():
    assert Instrument(first_waveform=0).effective_first_waveform == 0x09
    assert Instrument(first_waveform=0x11).effective_first_waveform == 0x11
    assert Instrument(first_waveform=0x40).effective_first_waveform == 0x40
    assert Instrument().effective_first_waveform == 0x09


def test_effective_first_waveform_keeps_raw_field():
    ins = Instrument(name=b"QS      ", control=0x1A, first_waveform=0)
    assert ins.first_waveform == 0
    assert ins.effective_first_waveform == 0x09


def test_zero_first_waveform_renders_editor_default_on_hr_frame():
    """A stored $00 first-waveform plays as $09: the HR test-bit frame writes
    CTRL = $09 AND ptn_gate, not $00; the raw byte still round-trips."""
    inst = Instrument(
        name=b"QS      ",
        control=0x1A,  # bit 3 (test-bit HR) set, like every quickscore instrument
        sustain=0xF,
        first_waveform=0,
        wf_table=bytes([0x41, 0x80, 0x00]),
    )
    pattern = Pattern(rows=[Row(note=49, instrument=1)] + [Row()] * 4, length=5)
    swm = SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pattern],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(3), straight_tempo(3))],
    )
    assert parse_swm(build_swm(swm)).instruments[0].first_waveform == 0

    p = SWMPlayer(swm)
    for _ in range(SETUP_FRAMES):
        p.play_frame()
    p.play_frame()
    v0 = p.voices[0]
    assert v0.in_hr_this_frame
    assert v0.sid_ctrl == (
        0x09 & v0.ptn_gate
    ), f"HR CTRL must be $09 & gate; got ${v0.sid_ctrl:02X} (gate ${v0.ptn_gate:02X})"
    assert v0.sid_ctrl & 0x08, "HR frame must hold the test bit"


def test_stored_nonzero_first_waveform_is_not_substituted():
    inst = Instrument(
        name=b"RAW     ",
        control=0x1A,
        sustain=0xF,
        first_waveform=0x40,
        wf_table=bytes([0x41, 0x80, 0x00]),
    )
    assert inst.effective_first_waveform == 0x40
    pattern = Pattern(rows=[Row(note=49, instrument=1)] + [Row()] * 4, length=5)
    swm = SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pattern],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(3), straight_tempo(3))],
    )
    p = SWMPlayer(swm)
    for _ in range(SETUP_FRAMES):
        p.play_frame()
    p.play_frame()
    assert p.voices[0].sid_ctrl == (0x40 & p.voices[0].ptn_gate)


def test_quickscore_native_render_uses_editor_first_waveform():
    """quickscore.swm stores $00 in all nine first-waveform bytes with HR
    enabled; the player must emit its HR CTRL byte as $09 & gate while
    ``build_swm`` still round-trips the stored $00."""
    data = quickscore_path().read_bytes()
    swm = parse_swm(data)
    assert len(swm.instruments) == 9
    for ins in swm.instruments:
        assert ins.first_waveform == 0
        assert ins.control & 0x08
    assert build_swm(swm) == data

    p = SWMPlayer(swm)
    hr_frames = 0
    for _ in range(3000):
        p.play_frame()
        for v in p.voices:
            if v.in_hr_this_frame:
                hr_frames += 1
                assert v.sid_ctrl == (
                    0x09 & v.ptn_gate
                ), f"HR CTRL must be $09 & gate; got ${v.sid_ctrl:02X} (gate ${v.ptn_gate:02X})"
    assert hr_frames, "quickscore must trigger HR frames within 3000 frames"
