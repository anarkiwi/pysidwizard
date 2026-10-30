"""Sequencer, WF/arp, chord, hard-restart and orderlist behaviour of :mod:`pysidwizard.player`."""

from __future__ import annotations

from itertools import groupby
from typing import Dict, List, Optional, Tuple

import pytest
from pysidtracker.playspec import SpecRuntimeError

from pysidwizard import (
    End,
    Instrument,
    Loop,
    MainVolume,
    Pattern,
    PlayPattern,
    Row,
    SubtuneJump,
    SWMFile,
    SWMPlayer,
    Transpose,
    parse_swm,
    straight_tempo,
)
from pysidwizard.constants import INST_WF_TABLE_POS
from tests._toy import SETUP_FRAMES, assert_6502, emu, grid, labels, note_word, toy_swm

# Row 0's TICK_2 runs on call SETUP_FRAMES; its WF row reaches the SID on the next call.
NOTE = SETUP_FRAMES + 1
WF_ROW = 3
PAL_CLOCK = 985248


def _inst(**fields) -> Instrument:
    """A sustained pulse instrument with ``fields`` overridden."""
    return Instrument(**{"name": b"TEST    ", "sustain": 0xF, "first_waveform": 0x41, **fields})


def _song(
    instruments: List[Instrument],
    patterns: List[List[Row]],
    sequences: Optional[list] = None,
    tempo: int = 3,
    subtunes: int = 1,
    **fields,
) -> SWMFile:
    """A module of ``patterns`` (one per row list), every voice on pattern 1 unless told."""
    return SWMFile(
        sequences=sequences or [[PlayPattern(1), End()]] * 3,
        patterns=[Pattern(rows=rows, length=len(rows)) for rows in patterns],
        instruments=instruments,
        subtune_tempos=[(straight_tempo(tempo), straight_tempo(tempo))] * subtunes,
        **fields,
    )


def _trace(
    swm: SWMFile, frames: int, *cells: str
) -> Tuple[SWMPlayer, List[List[int]], Dict[str, List[int]]]:
    """Per call: the SID registers and voice 0's ``cells``; each call's writes are the 6502's."""
    player = SWMPlayer(swm)
    cpu = emu(player)
    regs: List[List[int]] = []
    mem: Dict[str, List[int]] = {cell: [] for cell in cells}
    lab: Dict[str, int] = {}
    for call in range(frames):
        assert player.play_frame() == cpu.play_frame(), f"call {call} SID writes"
        lab = lab or labels(player)
        regs.append(list(player.regs))
        for cell in cells:
            mem[cell].append(player.memory[lab[cell]])
    return player, regs, mem


def _freq(regs: List[int], voice: int = 0) -> int:
    return regs[7 * voice] | regs[7 * voice + 1] << 8


def test_play_frame_reports_offset_diffs():
    """Call 0 reports all 25 registers as $00..$18 offsets, later calls only the changed ones.

    TICK_0 / TICK_1 walk the instrument header as WF rows: relative pitch 0, then SR $F0
    (-16, masked by ABSPTCH to index $70, past FREQTBL)."""
    player = SWMPlayer(toy_swm())
    first = player.play_frame()
    assert [r for r, _ in first] == list(range(25))
    word = note_word(player, 0)
    for base in (0, 7, 14):
        assert first[base : base + 2] == [(base, word & 0xFF), (base + 1, word >> 8)]
        assert first[base + 4] == (base + 4, 0x00)
    word = note_word(player, 0x70)
    assert player.play_frame() == [
        pair for base in (0, 7, 14) for pair in ((base, word & 0xFF), (base + 1, word >> 8))
    ]
    player.play_frame()
    player.play_frame()
    assert all(player.regs[base + 4] == 0x41 for base in (0, 7, 14))
    assert player.regs[24] & 0x0F == 0x0F


def test_toy_song_plays_its_notes_and_halts_at_the_orderlist_end():
    """Silence, middle C, its gate-off, a second note; at the End every call writes nothing
    and SPDCNT stays at the value that runs TICK_1 next (the one row 0's TICK_0 leaves)."""
    swm = toy_swm()
    player, regs, mem = _trace(swm, 24, "spdcnt")
    middle_c = note_word(player, 49)
    assert 260 < middle_c * PAL_CLOCK / (1 << 24) < 263
    freqs = [_freq(r) for r in regs]
    assert middle_c in freqs and note_word(player, 53) in freqs
    assert [ctrl for ctrl, _ in groupby(r[4] for r in regs)] == [0x00, 0x41, 0x40, 0x41]
    assert set(mem["spdcnt"][14:]) == {mem["spdcnt"][0]}
    assert regs[14:] == [regs[13]] * 10
    assert_6502(swm, 24)


def _closing_gate_off(sequence: list, subtunes: int = 1) -> SWMFile:
    """Tempo 3: a note row then a gate-off row, every voice playing ``sequence``."""
    inst = _inst(name=b"GATE    ", wf_table=b"\x41\x80\xff")
    return _song(
        [inst],
        [[Row(note=49, instrument=1), Row(note=0x7E)]],
        [sequence] * 3 * subtunes,
        subtunes=subtunes,
    )


# The gate-off row applies on its TICK_2: row 1's TICK_0 is call 3.
GATE_OFF_CALL = SETUP_FRAMES + 3


@pytest.mark.parametrize(
    "sequence,subtunes,plays",
    [
        ([PlayPattern(1), End()], 1, False),
        ([PlayPattern(1), Transpose(2), End()], 1, False),
        ([PlayPattern(1), PlayPattern(1), End()], 1, True),
        ([PlayPattern(1), Loop(position=0)], 1, True),
        ([PlayPattern(1), SubtuneJump(byte=0x81)], 2, True),
        ([PlayPattern(1), SubtuneJump(byte=0x85)], 1, True),
    ],
)
def test_last_row_plays_only_when_the_orderlist_goes_on(sequence, subtunes, plays):
    """PTN_SEQ meets an End on the tick after the last row's read and pins SPDCNT at TICK_1,
    so that row never reaches TICK_2; any jump (even to an absent subtune) plays it."""
    _, regs, _ = _trace(_closing_gate_off(sequence, subtunes), 16)
    ctrl = [r[4] for r in regs]
    assert ctrl[GATE_OFF_CALL - 1] & 1, ctrl
    assert (not ctrl[GATE_OFF_CALL] & 1) is plays, ctrl
    assert plays or all(c & 1 for c in ctrl[GATE_OFF_CALL:]), ctrl


def test_loop_onto_its_own_terminator_never_returns():
    """LOOPSEQ jumping to the $FF it stands on spins forever inside the play call."""
    with pytest.raises(SpecRuntimeError, match="ran past"):
        grid(_closing_gate_off([PlayPattern(1), Loop(position=1)]), 16)


def test_loop_to_the_start_keeps_every_call_finite():
    """A one-row pattern looped by ``Loop(0)`` at tempo 1 re-enters the orderlist each call."""
    inst = _inst(name=b"LP      ")
    assert_6502(
        _song([inst], [[Row(note=49, instrument=1)]], [[PlayPattern(1), Loop(0)]] * 3, tempo=1), 40
    )


@pytest.mark.parametrize(
    "note,arp,index",
    [
        (49, 0x00, 49),
        (49, 0x03, 52),
        (49, 0x7E, (49 + 0x7E) & 0x7F),
        (62, 0x7E, (62 + 0x7E) & 0x7F),
        (49, 0x30, 97),
        (49, 0x81, 0x01),
        (49, 0xC4, 0x44),
        (49, 0xDF, 0x5F),
        (49, 0xE0, 49 - 0x20),
        (49, 0xFF, 48),
    ],
)
def test_wf_arp_byte_picks_the_frequency_table_index(note, arp, index):
    """WFARPTB: $00..$7E relative up, $81..$DF absolute, $E0..$FF relative down; ABSPTCH's
    ``and #$7F`` wraps rather than clamps, an index past 95 reads on past FREQTBL/FREQTBH, and
    RELPTCH's ``adc`` carry reaches WRPITCH's ``adc DETUNER``."""
    swm = toy_swm()
    swm.instruments[0].wf_table = bytes([0x41, arp, 0x00])
    swm.patterns[0].rows[0] = Row(note=note, instrument=1)
    player, regs, _ = _trace(swm, NOTE + 1)
    carry = (arp < 0x80 or arp >= 0xE0) and note + arp > 0xFF
    assert _freq(regs[NOTE]) == note_word(player, index) + carry


def test_filter_set_row_unpacks_band_resonance_cutoff():
    """A SET row ($80+): bits 4-6 the $D418 band, bits 0-3 the $D417 resonance, then the
    cutoff high byte with the low byte cleared; routing is the filtered voice only."""
    band_res, cutoff = 0x94, 0x20
    filtered = _inst(wf_table=bytes([0x41, 0x80, 0x00]), filter_table=bytes([band_res, cutoff, 0]))
    plain = _inst(wf_table=bytes([0x41, 0x80, 0x00]))
    swm = _song(
        [filtered, plain],
        [[Row(note=49, instrument=i)] + [Row()] * 4 for i in (1, 2)],
        [[PlayPattern(1), End()], [PlayPattern(2), End()], [PlayPattern(2), End()]],
    )
    _, regs, _ = _trace(swm, NOTE + 1)
    assert regs[NOTE][21:25] == [
        0,
        cutoff,
        (band_res & 0x0F) << 4 | 0b001,
        band_res & 0x70 | 0x0F,
    ]
    assert_6502(swm, NOTE + 1)


def _chord_swm(wf_table: bytes, chord_table: bytes, default_chord: int = 1) -> SWMFile:
    inst = _inst(default_chord=default_chord, wf_table=wf_table)
    return _song([inst], [[Row(note=49, instrument=1)] + [Row()] * 12], chord_table=chord_table)


def test_chord_with_loop_terminator_cycles_its_intervals():
    """A $7F WF row plays one chord interval per call; the chord's $7F restarts it."""
    swm = _chord_swm(bytes([0x41, 0x7F, 0x00, 0xFE, 0x10, 0x00]), bytes([3, 7, 12, 0x7F]))
    player, regs, _ = _trace(swm, NOTE + 9)
    cycle = [note_word(player, 49 + k) for k in (3, 7, 12)]
    assert [_freq(r) for r in regs[NOTE:]] == cycle * 3


def test_chord_return_terminator_continues_with_the_next_wf_row():
    """Chord 2 ``[5, $7E]``: one call at +5, then $7E hands over to the WF row after the $7F."""
    swm = _chord_swm(
        bytes([0x41, 0x7F, 0x00, 0x21, 0x00, 0x00]), bytes([0x7E, 5, 0x7E]), default_chord=2
    )
    player, regs, _ = _trace(swm, NOTE + 2)
    assert [(_freq(r), r[4]) for r in regs[NOTE:]] == [
        (note_word(player, 54), 0x41),
        (note_word(player, 49), 0x21),
    ]


@pytest.mark.parametrize("detune,chord,interval", [(0x10, 3, 3), (0xFF, 3, 3), (0x10, 0x7E, 0)])
def test_chord_row_loads_detune_without_nop_test(detune, chord, interval):
    """PLYCHRD stores a $7F row's detune byte as is, $FF included, also when the chord returns
    ($7E); WRPITCH adds DETUNER to the note's word."""
    swm = _chord_swm(bytes([0x41, 0x7F, detune, 0x41, 0x00, 0xFF]), bytes([chord, 0x7F]))
    player, regs, mem = _trace(swm, NOTE + 1, "detuner")
    assert mem["detuner"][NOTE] == detune
    assert _freq(regs[NOTE]) == note_word(player, 49 + interval) + detune


def test_hard_restart_holds_the_test_bit_for_one_call():
    """A control-$08 instrument's first note (setup calls silent) runs HR: its TICK_2 writes
    first_waveform $09 with the post-HR AD/SR, the next call the WF row's waveform."""
    inst = _inst(
        control=0x08,
        hr_decay=0xF,
        attack=1,
        decay=2,
        sustain=0xA,
        release=0xA,
        first_waveform=0x09,
        wf_table=bytes([0x41, 0x80, 0x00]),
    )
    swm = _song([inst], [[Row(note=49, instrument=1)] + [Row()] * 4])
    _, regs, _ = _trace(swm, NOTE + 1)
    assert [r[4] for r in regs[:SETUP_FRAMES]] == [0] * SETUP_FRAMES
    assert [r[4:7] for r in regs[SETUP_FRAMES:]] == [[0x09, 0x12, 0xAA], [0x41, 0x12, 0xAA]]
    assert_6502(swm, NOTE + 1)


def test_note_start_resets_the_wf_table_position_before_walking_it():
    """Row 1's note restarts WFTPOS at the table's start on its TICK_2; the walk resumes next."""
    inst = _inst(wf_table=bytes([0x41, 0x80, 0x00, 0x21, 0x80, 0x00]))
    rows = [Row(note=49, instrument=1), Row(note=53, instrument=1)] + [Row()] * 6
    _, _, mem = _trace(_song([inst], [rows], tempo=4), SETUP_FRAMES + 6, "wftpos")
    start = SETUP_FRAMES + 4
    assert mem["wftpos"][start - 1] != INST_WF_TABLE_POS
    assert mem["wftpos"][start : start + 2] == [INST_WF_TABLE_POS, INST_WF_TABLE_POS + WF_ROW]


def test_untriggered_voices_stay_silent():
    """Voices whose pattern has no note never write their $D407-$D414 block."""
    inst = _inst(wf_table=bytes([0x41, 0x80, 0x00]))
    swm = _song(
        [inst],
        [[Row(note=49, instrument=1), Row()], [Row()]],
        [[PlayPattern(1), End()], [PlayPattern(2), End()], [PlayPattern(2), End()]],
    )
    _, regs, _ = _trace(swm, NOTE + 2)
    assert all(r[7:21] == [0] * 14 for r in regs)
    assert regs[-1][4] == 0x41 and _freq(regs[-1])


def test_voice_starts_writing_once_its_note_row_is_read():
    """A voice empty on row 0 writes nothing until row 1's TICK_0 reads its note."""
    inst = _inst(wf_table=bytes([0x41, 0x80, 0x00]))
    swm = _song([inst], [[Row(), Row(note=49, instrument=1), Row()]])
    _, regs, _ = _trace(swm, SETUP_FRAMES + 5)
    row1 = SETUP_FRAMES + 1
    assert all(r[:7] == [0] * 7 for r in regs[:row1])
    assert all(_freq(r) for r in regs[row1:]) and regs[-1][4] == 0x41


def test_subtune_jump_terminator_continues_in_the_target_subtune():
    """$FF then a high-bit byte (LOOPSEQ -> SETSEQA) moves every voice to subtune 1's orderlist."""
    inst = _inst(name=b"SJ      ", wf_table=bytes([0x41, 0x00, 0xFF]))
    swm = _song(
        [inst],
        [[Row(note=49, instrument=1)], [Row(note=61, instrument=1)]],
        [[PlayPattern(1), SubtuneJump(byte=0x81)]] * 3 + [[PlayPattern(2), Loop(0)]] * 3,
        tempo=4,
        subtunes=2,
    )
    player, regs, mem = _trace(swm, 14, "curnot")
    freqs = {_freq(r) for r in regs}
    assert {note_word(player, 49), note_word(player, 61)} <= freqs
    assert mem["curnot"][-1] == 61


@pytest.mark.parametrize("volume", [5, None])
def test_orderlist_main_volume_lands_with_the_next_patterns_first_row(volume):
    """SEQVOLU -> MAINVOL: $D418's volume changes on the call reading pattern 2's row 0
    (its note 53 entering CURNOT), never earlier; without the seq-fx it stays $F."""
    voice0 = [PlayPattern(1)] + ([MainVolume(volume)] if volume is not None else [])
    rest = [PlayPattern(2), End()]
    swm = _song(
        [_inst(name=b"VOL     ")],
        [[Row(note=49, instrument=1), Row()], [Row(note=53, instrument=1), Row()]],
        [voice0 + rest] + [[PlayPattern(1)] + rest] * 2,
        tempo=2,
    )
    _, regs, mem = _trace(swm, 12, "curnot")
    read = mem["curnot"].index(53)
    vols = [r[24] & 0x0F for r in regs]
    assert vols == [0x0F] * read + [0x0F if volume is None else volume] * (12 - read)


@pytest.mark.parametrize(
    "control,gate_off_tick0,gate_off_tick1",
    [(0x1A, True, True), (0x1D, False, True), (0x18, False, False)],
)
def test_pre_hr_gate_off_only_on_firing_tick(control, gate_off_tick0, gate_off_tick1):
    """ISHARDR ANDs the control byte with $02 at TICK_0 (HARDRST) / $01 at TICK_1 (HRDR1FR);
    HRGTOFF's PTNGATE=$FE runs on the firing tick only, so the previous note keeps its gate
    until then."""
    inst = _inst(
        control=control, hr_decay=0xF, wf_table=bytes([0x41, 0x80, 0x00, 0xFE, 0x10, 0x00])
    )
    rows = [Row(note=49, instrument=1), Row(), Row(note=53, instrument=1), Row()]
    tick0 = SETUP_FRAMES + 4
    _, regs, mem = _trace(_song([inst], [rows]), tick0 + 2, "ptngate")
    for call, gate_off in ((tick0, gate_off_tick0), (tick0 + 1, gate_off_tick1)):
        assert (not regs[call][4] & 1) is gate_off, (control, call, regs[call][4])
        assert (mem["ptngate"][call] == 0xFE) is gate_off, (control, call, mem["ptngate"][call])


def test_instrument_slot_above_the_count_plays_as_the_6502():
    """A row naming an absent instrument reads whatever SID-Maker's pointer table holds there."""
    swm = toy_swm()
    swm.patterns[0].rows[0] = Row(note=49, instrument=2)
    assert_6502(swm, 8)


def test_issue57_module_plays_as_the_6502(oob_sample_bytes: bytes):
    """issue #57: a module whose rows name instruments above its count."""
    assert_6502(parse_swm(oob_sample_bytes), 100)
