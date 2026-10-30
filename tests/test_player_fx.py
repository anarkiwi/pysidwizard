"""Effects, tables, tempo and build-flag behaviour of :mod:`pysidwizard.player` on toy modules."""

from __future__ import annotations

import dataclasses
from itertools import pairwise

import pytest

from pysidwizard import (
    End,
    Instrument,
    Pattern,
    PlayPattern,
    Row,
    SWMFile,
    SWMPlayer,
    Transpose,
    playspec,
    straight_tempo,
)
from pysidwizard.constants import INST_WF_TABLE_POS
from tests._toy import SETUP_FRAMES, assert_6502, grid, labels, note_word, toy_swm

# Row 0's TICK_2 runs on call SETUP_FRAMES; its table walk reaches the SID on the next call.
NOTE = SETUP_FRAMES + 1


def _ins(swm: SWMFile, **fields) -> SWMFile:
    """``swm`` with its instrument's ``fields`` replaced."""
    swm.instruments[0] = dataclasses.replace(swm.instruments[0], **fields)
    return swm


def _rows(swm: SWMFile, **rows) -> SWMFile:
    """``swm`` with pattern rows ``r<i>`` replaced."""
    for key, row in rows.items():
        swm.patterns[0].rows[int(key[1:])] = row
    return swm


def _fx_swm(fx: int, fx_value=None, **kwargs) -> SWMFile:
    """``toy_swm(**kwargs)`` with the given effect column on row 0."""
    swm = toy_swm(**kwargs)
    swm.patterns[0].rows[0] = Row(note=49, instrument=1, fx=fx, fx_value=fx_value)
    return swm


def _run(swm: SWMFile, calls: int = NOTE) -> SWMPlayer:
    """``swm``'s player after ``calls`` play calls."""
    p = SWMPlayer(swm)
    for _ in range(calls):
        p.play_frame()
    return p


def _cell(p: SWMPlayer, name: str, voice: int = 0) -> int:
    """Voice ``voice``'s byte of the per-voice variable ``name``."""
    return p.memory[labels(p)[name] + voice * playspec.VOICE_STRIDE]


def _regs(swm: SWMFile, call: int = NOTE) -> list:
    """The SID registers after play call ``call``."""
    return grid(swm, call + 1)[call]


def _one_voice(swm: SWMFile, rows=None) -> SWMFile:
    """``swm`` whose voices 1-2 play ``rows`` (default: the toy pattern) as pattern 2."""
    rows = toy_swm().patterns[0].rows if rows is None else rows
    swm.patterns.append(Pattern(rows=list(rows), length=len(rows)))
    swm.sequences = [[PlayPattern(1), End()], [PlayPattern(2), End()], [PlayPattern(2), End()]]
    return swm


def test_bare_instrument_select_walks_the_instrument_header():
    """A bare instrument select leaves WFTPOS / PWTPOS at 0: the tables read the header.

    PWSWEEP reads header byte 0 (control $0A) as the sweep count and byte 1 (HR-AD $0F) as
    the PW-low delta; RDWFROW reads $0A as an arp-speed byte, so no waveform is written.
    """
    inst = Instrument(
        name=b"STALE   ",
        control=0x0A,
        hr_decay=0x0F,
        hr_sustain=0x0F,
        sustain=0xF,
        first_waveform=0x41,
        wf_table=bytes([0x41, 0x80, 0x00]),
    )
    swm = SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[Pattern(rows=[Row(instrument=1)] + [Row()] * 7, length=8)],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(8), straight_tempo(8))],
    )
    assert_6502(swm, SETUP_FRAMES + 10)
    rows = grid(swm, SETUP_FRAMES + 10)[SETUP_FRAMES:]
    assert [row[2] for row in rows] == [0x0F * (k + 1) for k in range(10)]
    assert {row[4] for row in rows} == {0}
    freqs = {(row[0], row[1]) for row in rows}
    assert len(freqs) == 1 and freqs != {(0, 0)}


@pytest.mark.parametrize(
    "fx,value,register,expected",
    [
        (0x04, 0x21, 4, 0x21),
        (0x05, 0xA3, 5, 0xA3),
        (0x06, 0x5C, 6, 0x5C),
        (0x0F, 0xC4, 0x16, 0xC4),
        (0x1F, 0x38, 0x17, 0x38),
        (0xA7, None, 0x18, 0x07),
        (0xB2, None, 0x18, 0x2F),
        (0xF9, None, 0x17, 0x90),
    ],
)
def test_a_register_fx_on_the_note_row_reaches_the_sid(fx, value, register, expected):
    """WRITEWF/AD/SR, BIGFX0F/1F and SMALFXA/B/F land after STRTSND's own writes."""
    assert _regs(_fx_swm(0x02, 0x00), SETUP_FRAMES)[register] != expected
    assert _regs(_fx_swm(fx, value), SETUP_FRAMES)[register] == expected


def test_register_fx_apply_on_a_note_less_row_and_the_wf_table_takes_ctrl_back():
    """PATT_FX runs on note-less rows; the WF table owns the waveform once it walks again."""
    swm = _fx_swm(0x04, 0x21)
    swm.patterns[0].rows[1] = Row(fx=0x06, fx_value=0x39)
    assert_6502(swm, NOTE + 4)
    regs = _regs(swm, NOTE + 3)
    assert (regs[6], regs[4]) == (0x39, 0x41)


@pytest.mark.parametrize("code,ctrl", [(0x48, 0x81), (0xEA, 0x41)])
def test_small_fx_4_sets_the_waveform_nibble_and_small_fx_e_stores_nothing(code, ctrl):
    """SMALFX4 merges over WFGHOST's high nibble keeping the gate; SMALFXE's store is gone."""
    swm = _fx_swm(0x02, 0x00)
    assert _regs(swm, NOTE + 3)[4] == 0x41
    swm.patterns[0].rows[1] = Row(fx=code)
    assert _regs(swm, NOTE + 3)[4] == ctrl


def test_filter_routing_bit_follows_the_instrument_filter_table():
    """STRTSND's SETFLTP ORs each voice's routing bit into FSWITCH."""
    swm = toy_swm()
    swm.instruments[0].filter_table = b"\x80\x40\x00"
    assert _regs(swm)[0x17] & 0x07 == 0x07


def _tables_swm(fx: int, fx_value: int) -> SWMFile:
    """``_fx_swm`` whose instrument has two-row WF / PW tables and two chords."""
    swm = _fx_swm(fx, fx_value)
    ins = swm.instruments[0]
    ins.wf_table = bytes([0x41, 0x80, 0x00, 0x11, 0x80, 0x00])
    ins.pw_table = bytes([0x88, 0x00, 0x00, 0x84, 0x00, 0x00])
    ins.arp_speed = 0x02
    swm.chord_table = bytes([0x00, 0x04, 0x07, 0x7F, 0x00, 0x03, 0x07, 0x7F])
    return swm


_PW_BASE = INST_WF_TABLE_POS + 6 + 1
# The toy instrument's PW table: after its one WF row and that table's terminator.
_PW_ROW_0 = INST_WF_TABLE_POS + 3 + 1


@pytest.mark.parametrize(
    "fx,value,cells",
    [
        (0x09, 0x01, {"wftpos": INST_WF_TABLE_POS + 3}),
        (0x0A, 0x01, {"pwtpos": _PW_BASE + 3}),
        (0x0C, 0x05, {"arpsped": 0x05, "arpscnt": 0xFF}),
        (0xC3, None, {"arpsped": 0x03, "arpscnt": 0xFF}),
        (0x07, 0x02, {"curchord": 0x02, "chordpos": 4}),
        (0x0D, 0x05, {"detuner": 0x05}),
        (0x0D, 0xFB, {"detuner": 0xFB}),
        (0xD2, None, {"detuner": 0x10}),
        (0x0E, 0x3A, {"pwhigho": 0x0A}),
        (0x16, 0x37, {"slidevib": 0x30}),
        (0x93, None, {"vibfrequ": 6}),
    ],
)
def test_a_table_or_voice_fx_sets_its_variable(fx, value, cells):
    """BIGFX09/0A table jumps, ARPSPED, CURCHORD, DETUNER, PWHIGHO, SLIDEVIB and VIBFREQU."""
    p = _run(_tables_swm(fx, value))
    assert {name: _cell(p, name) for name in cells} == cells


def test_small_fx_8_overrides_the_vibrato_amplitude_and_big_fx_08_0_clears_freqmod():
    """VIBAMFX keeps the instrument's frequency nibble; SETFMOD stores 0 for amplitude 0."""
    swm = _fx_swm(0x84)
    swm.instruments[0].vibrato = 0x25
    p = _run(swm)
    assert _cell(p, "vibfrequ") == 10
    assert (_cell(p, "freqmodl"), _cell(p, "freqmodh")) != (0, 0)
    swm = _fx_swm(0x08, 0x07)
    swm.instruments[0].vibrato = 0x84
    p = _run(swm)
    assert (_cell(p, "freqmodl"), _cell(p, "freqmodh"), _cell(p, "vibfrequ")) == (0, 0, 14)


@pytest.mark.parametrize("fx,reg", [(0x05, 5), (0x06, 6)])
def test_write_adsr_reaches_the_sid_on_a_voice_with_no_instrument(fx, reg):
    """CURINS=0 sends TICK_2 to LEGATOO, whose INSPTFX writes SIDG.AD/SR."""
    swm = toy_swm()
    swm.patterns[0].rows[0] = Row(note=0x7E, fx=fx, fx_value=0x9C)
    assert_6502(swm, NOTE + 1)
    regs = _regs(swm)
    assert (regs[reg], regs[11 - reg]) == (0x9C, 0)


def test_write_sr_reaches_the_sid_on_a_legato_row():
    """A legato row's LEGATOO runs INSPTFX, so WRITESR lands on row 3's TICK_2."""
    swm = toy_swm()
    swm.patterns[0].rows[3] = Row(note=53, instrument=0x3F, fx=0x06, fx_value=0x5A)
    sr = [row[6] for row in grid(swm, NOTE + 12)]
    assert sr[NOTE + 8] == 0x5A and 0x5A not in sr[: NOTE + 8]


@pytest.mark.parametrize("fx", [0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C])
def test_big_fx_1c_shifts_the_cutoff_and_17_to_1b_alias_it(fx):
    """FLSHIFT adds to the cutoff ghost; BIGFX17..1B are labels falling into BIGFX1C."""
    swm = _fx_swm(0x0F, 0x40)
    swm.patterns[0].rows[1] = Row(fx=fx, fx_value=0x10)
    swm.patterns[0].rows[2] = Row(fx=fx, fx_value=0xF0)
    cutoff = [row[0x16] for row in grid(swm, NOTE + 7)]
    assert cutoff[NOTE::3] == [0x40, 0x50, 0x30]


def _row_lengths(swm: SWMFile, calls: int = 14) -> list:
    """Each voice's first three row lengths: the SPDCNT each row reaches before it restarts at 1."""
    p = SWMPlayer(swm)
    counts = []
    for _ in range(calls):
        p.play_frame()
        counts.append([_cell(p, "spdcnt", v) for v in range(3)])
    return [[a[v] for a, b in pairwise(counts) if b[v] == 1][:3] for v in range(3)]


@pytest.mark.parametrize(
    "fx,value,voice_0,others",
    [
        (0x10, 0x04, [4, 4, 4], [4, 4, 4]),
        (0x11, 0x53, [5, 3, 5], [5, 3, 5]),
        (0x13, 0x04, [4, 4, 4], [3, 3, 3]),
        (0x14, 0x53, [5, 3, 5], [3, 3, 3]),
    ],
)
def test_tempo_fx_set_the_main_or_one_track_tempo(fx, value, voice_0, others):
    """BIGFX10 pins one main tempo, BIGFX11 splits the byte into a funk pair; 13/14 one track."""
    swm = _one_voice(_fx_swm(fx, value))
    assert _row_lengths(swm) == [voice_0, others, others]


def test_a_track_tempo_set_at_tick_2_rows_from_the_next_frame():
    """The frame-start ``sbc TEMPOTBL`` already sees the TICK_2 fx's tempo."""
    swm = _fx_swm(0x13, 0x03, tempo=6)
    swm.patterns[0].rows[1] = Row(note=53, instrument=1)
    assert_6502(swm, NOTE + 6)
    hi = [row[1] for row in grid(swm, NOTE + 6)]
    assert hi.index(note_word(_run(swm), 53) >> 8) == NOTE + 3


def test_big_fx_03_without_a_note_rearms_the_slide():
    """BIGFX03 is SETSLID + SETFMOD: FREQMOD is EXPTABH[ceil(value/2) + DPITCH]."""
    swm = _fx_swm(0x02, 0x10)
    swm.patterns[0].rows[1] = Row(fx=0x03, fx_value=0x20)
    p = _run(swm, NOTE + 3)
    exptabh = labels(p)["exptabh"]
    assert _cell(p, "slidevib") == 0x83
    assert (_cell(p, "freqmodl"), _cell(p, "freqmodh")) == (p.memory[exptabh + 0x10 + 49], 0)


@pytest.mark.parametrize("value,zero", [(0x00, True), (0x01, False)])
def test_a_zero_slide_speed_is_a_zero_freqmod(value, zero):
    """SETFMOD's ``beq wrFmodL`` stores FREQMOD 0 for a speed of 0."""
    p = _run(_fx_swm(0x03, value))
    assert ((_cell(p, "freqmodl"), _cell(p, "freqmodh")) == (0, 0)) is zero


@pytest.mark.parametrize("driver_type", [0, 3])
def test_delay_fx_is_a_bare_rts_outside_the_extra_build(driver_type):
    """BIGFX1D/1E assemble to ``rts`` unless DELAYSUPPORT_ON (Extra)."""
    swm = _fx_swm(0x1E, 0x04, driver_type=driver_type)
    assert_6502(swm, NOTE + 6)
    same = grid(swm, NOTE + 6) == grid(_fx_swm(0x02, 0x00, driver_type=driver_type), NOTE + 6)
    assert same is (driver_type != 3)


@pytest.mark.parametrize("tempo", [1, 2])
def test_extra_build_advances_rows_at_tempo_1_and_2(tempo):
    """Only FASTSPEEDBIND (Extra) reaches TICK_2 below tempo 3; elsewhere PTNGATE masks CTRL.

    Extra applies row 3 on its call ``tempo - 1`` and the WF table walk sounds it a call later.
    """
    silent = grid(toy_swm(tempo), 16)
    assert {row[ctrl] for row in silent for ctrl in (4, 11, 18)} == {0}
    swm = toy_swm(tempo, driver_type=3)
    assert_6502(swm, 16)
    hi = [row[1] for row in grid(swm, 16)]
    assert hi.index(note_word(_run(swm, 1), 53) >> 8) == 3 * tempo + (tempo - 1) + 1


def test_allghostregs_combines_two_adsr_small_fx_in_one_row():
    """Under ALLGHOSTREGS (Extra) SMALFX5/6 read SIDG.SR, so a sustain survives a release fx."""
    swm = toy_swm()
    swm.patterns[0].rows[1] = Row(instrument=0x50)
    swm.patterns[0].rows[2] = Row(instrument=0x69)
    assert _regs(swm, NOTE + 6)[6] == 0xF9
    assert _regs(dataclasses.replace(swm, driver_type=3), NOTE + 6)[6] == 0x09


def test_hardrestypes_off_uses_the_default_hr_adsr():
    """Medium has no per-instrument HR ADSR: defauHR writes $0F00."""
    swm = _ins(
        toy_swm(6), hr_attack=0xA, hr_decay=0xB, hr_sustain=0xC, hr_release=0xD, control=0x1A
    )
    regs = _regs(swm, NOTE + 16)
    assert (regs[5], regs[6]) == (0xAB, 0xCD)
    swm.driver_type = 1
    assert_6502(swm, NOTE + 18)
    regs = _regs(swm, NOTE + 16)
    assert (regs[5], regs[6]) == (0x0F, 0x00)


def test_frame1switch_off_always_starts_the_note_on_waveform_09():
    """Medium's STRTSND drops the control ``and #8`` test and writes ``lda #$09``."""
    swm = toy_swm()
    swm.instruments[0].control = 0x12
    swm.instruments[0].first_waveform = 0x89
    assert _regs(swm, SETUP_FRAMES)[4] != 0x09
    swm.driver_type = 1
    assert _regs(swm, SETUP_FRAMES)[4] == 0x09


def test_filt_ctrl_fx_off_makes_big_fx_1f_a_bare_rts():
    """Medium leaves the BIGFX1F body out."""
    assert _regs(_fx_swm(0x1F, 0x35), NOTE + 3)[0x17] == 0x35
    assert _regs(_fx_swm(0x1F, 0x35, driver_type=1), NOTE + 3)[0x17] != 0x35


def _light(swm: SWMFile) -> SWMFile:
    """``swm`` exported as SID-Maker 1.7's Light build."""
    swm.driver_type, swm.player_version = 2, "1.7"
    return swm


def test_octaveshift_and_transpose_are_left_out_of_the_light_build():
    """CLEGATO's octave-field add and ``adc TRANSP,x`` are assembled out of Light."""
    swm = toy_swm()
    swm.instruments[0].octave_shift = 12
    swm.sequences = [[Transpose(-5), PlayPattern(1), End()]] * 3
    p = _run(swm, NOTE + 1)
    assert _cell(p, "dpitch") == 49 + 12 - 5
    assert _regs(swm, NOTE + 1)[1] == note_word(p, 49 + 12 - 5) >> 8
    _light(swm)
    p = assert_6502(swm, NOTE + 2)
    assert _cell(p, "dpitch") == 49
    assert _regs(swm, NOTE + 1)[1] == note_word(p, 49) >> 8


def test_detunesupport_off_drops_the_detune_add_and_its_carry():
    """Light's WRPITCH loses both ``adc DETUNER,x`` and ``adc #0``."""
    swm = _fx_swm(0x0D, 0x40)
    word = note_word(_run(swm, 1), 49)
    assert _regs(swm, NOTE + 3)[0] == ((word & 0xFF) + 0x40 + 1) & 0xFF
    assert _regs(_light(swm), NOTE + 3)[0] == word & 0xFF


def test_finefiltsweep_off_makes_the_filter_sweep_an_8_bit_add():
    """Without CTFL_GHO the delta lands straight on $D416: an 8x faster sweep."""
    swm = toy_swm()
    swm.instruments[0].filter_table = bytes([0x91, 0x40, 0x00, 0x04, 0x08, 0x00])
    fine = _regs(swm, NOTE + 5)[0x16]
    assert _regs(_light(swm), NOTE + 5)[0x16] > fine


def test_calcvibrato_off_makes_the_vibrato_amplitude_pitch_independent():
    """Light's SETFMOD indexes FREQTBH-1 by the amplitude alone, not ``lsr ; adc DPITCH``."""
    swm = toy_swm()
    swm.instruments[0].vibrato = 0x41
    _one_voice(swm, [Row(note=20, instrument=1), Row(), Row()])
    mods = []
    for build in (swm, _light(dataclasses.replace(swm))):
        p = _run(build, NOTE + 4)
        mods.append({(_cell(p, "freqmodl", v), _cell(p, "freqmodh", v)) for v in (0, 1)})
    assert [len(m) for m in mods] == [2, 1]


@pytest.mark.parametrize("control,counter", [(0x10, 3), (0x20, 6), (0x30, 0)])
def test_setvibr_starts_vibracnt_by_vibrato_type(control, counter):
    """SETVIBR starts VIBRACNT a quarter period in, half for type $20 and none for $30."""
    swm = toy_swm()
    swm.instruments[0].control = control
    swm.instruments[0].vibrato = 0x46
    p = _run(swm)
    assert (_cell(p, "slidevib"), _cell(p, "vibfrequ"), _cell(p, "vibracnt")) == (
        control,
        12,
        counter,
    )


def test_a_vibrato_delay_with_bit_7_starts_at_once():
    """SETVIB0 stores the delay byte whole; NORMVIB's ``bmi DOVIBRA`` tests bit 7."""
    swm = toy_swm()
    swm.instruments[0].vibrato_delay = 0x80
    assert _cell(_run(swm), "videlcnt") & 0x80


def test_the_slot_0_instrument_is_the_one_curins_0_walks():
    """A note with no instrument ever walks the slot-0 header: arp 0 from table index 0."""
    swm = toy_swm()
    swm.patterns[0].rows[0] = Row(note=49)
    p = assert_6502(swm, NOTE + 1)
    regs = _regs(swm, 0)
    assert regs[0] | regs[1] << 8 == note_word(p, 0)


def _curins0_swm(row: Row, **instrument) -> SWMFile:
    """``toy_swm`` whose voice 1 plays ``row`` with no instrument ever selected."""
    swm = toy_swm()
    swm.instruments[0] = dataclasses.replace(swm.instruments[0], **instrument)
    swm.patterns.append(Pattern(rows=[row, Row(), Row()], length=3))
    swm.sequences = [[PlayPattern(1), End()], [PlayPattern(2), End()], [PlayPattern(2), End()]]
    return swm


@pytest.mark.parametrize(
    "code,reg,value",
    [(0x6F, 6, 0x0F), (0x2C, 5, 0xC0), (0x3C, 5, 0x0C), (0x5C, 6, 0xC0), (0x6C, 6, 0x0C)],
)
def test_curins_0_adsr_small_fx_merges_the_inits_playerzp(code, reg, value):
    """With CURINS=0, SETINBH / SETINBL read the init's PLAYERZP that RETAINZEROPAGE restores.

    The other nibble is 0 from there, not the ADSR of the instrument voice 0 loaded.
    """
    swm = _curins0_swm(Row(fx=code), attack=5, decay=7, sustain=9, release=1)
    p = assert_6502(swm, NOTE + 1)
    assert playspec.binding(p.view(), p.entry)["retainzeropage"]
    assert _regs(swm)[7 + reg] == value


@pytest.mark.parametrize(
    "pointers,gate,ctrl", [({"gateoff_wf": 0x16}, 0xFF, 0x21), ({}, 0xFE, 0x40)]
)
def test_a_gate_off_wf_pointer_jumps_the_wf_table_and_keeps_the_gate(pointers, gate, ctrl):
    """NGATEOF's ``bne SETGOFF`` moves WFTPOS instead of clearing PTNGATE's gate bit."""
    swm = toy_swm()
    ins = swm.instruments[0]
    ins.wf_table = bytes([0x41, 0x00, 0x00, 0xFE, 0x10, 0x00, 0x21, 0x00, 0x00, 0xFE, 0x16, 0x00])
    ins.pw_table = bytes([0x88, 0x00, 0x00, 0x84, 0x00, 0x00])
    for name, value in pointers.items():
        setattr(ins, name, value)
    p = assert_6502(swm, NOTE + 8)
    assert (_cell(p, "ptngate"), _regs(swm, NOTE + 8)[4]) == (gate, ctrl)


@pytest.mark.parametrize("pointer,pw_hi", [(0, 0x04), (_PW_ROW_0, 0x08)])
def test_a_gate_off_pw_pointer_rewinds_the_pw_table(pointer, pw_hi):
    """chPWgof sets PWTPOS from the gate-off PW pointer, so the gate-off row replays PW row 0."""
    swm = toy_swm()
    ins = swm.instruments[0]
    ins.pw_table = bytes([0x88, 0x00, 0x00, 0x84, 0x00, 0x00])
    ins.gateoff_pw = pointer
    gate_off = 2 * 3 + SETUP_FRAMES
    assert_6502(swm, gate_off + 2)
    assert [row[3] for row in grid(swm, gate_off + 2)[gate_off - 1 :]] == [0x04, pw_hi, pw_hi]


def _chord_swm(note: int, chord: bytes) -> SWMFile:
    """``toy_swm`` whose instrument plays chord 1 (``chord``)."""
    swm = toy_swm()
    swm.instruments[0].wf_table = bytes([0x41, 0x7F, 0x00])
    swm.instruments[0].default_chord = 1
    swm.chord_table = chord
    swm.patterns[0].rows[0] = Row(note=note, instrument=1)
    return swm


def _wf_arp_swm(note: int, arp: int, transpose: int = 0) -> SWMFile:
    """``toy_swm`` whose single WF row applies ``arp`` to ``note``."""
    swm = toy_swm()
    swm.instruments[0].wf_table = bytes([0x41, arp, 0x00])
    swm.patterns[0].rows[0] = Row(note=note, instrument=1)
    if transpose:
        swm.sequences = [[Transpose(transpose), PlayPattern(1), End()]] * 3
    return swm


@pytest.mark.parametrize(
    "swm,carry",
    [
        (_chord_swm(49, bytes([0xFB, 0x7F])), 1),
        (_chord_swm(49, bytes([0x03, 0x7F])), 0),
        (_wf_arp_swm(5, 0x0C, -16), 1),
        (_wf_arp_swm(5, 0x0C), 0),
    ],
)
def test_relptch_carries_any_arp_plus_dpitch_past_ff(swm, carry):
    """RELPTCH's ``clc ; adc DPITCH`` carry reaches WRPITCH's ``adc DETUNER``."""
    p = assert_6502(swm, NOTE + 1)
    assert _regs(swm)[0] == (_cell(p, "freqlo") + carry) & 0xFF


def test_a_bare_instrument_select_keeps_the_held_dpitch():
    """CLEGATO latches DPITCH at the note; the next note takes the new octave shift."""
    swm = toy_swm()
    swm.instruments.append(dataclasses.replace(swm.instruments[0], octave_shift=12))
    swm.patterns[0] = Pattern(
        rows=[Row(note=49, instrument=1), Row(instrument=2), Row(note=49), Row(), Row()],
        length=5,
    )
    p = assert_6502(swm, 16)
    hi = [row[1] for row in grid(swm, 16)]
    assert hi[NOTE : NOTE + 6] == [note_word(p, 49) >> 8] * 6
    assert hi[NOTE + 6 :] == [note_word(p, 61) >> 8] * (16 - NOTE - 6)


def test_fladvan_reads_a_8x_third_column_as_fswitch():
    """FLADVAN: from 1.4 a $80-$8F keyboard-track column is FSWITCH's routing nibble."""
    swm = toy_swm()
    swm.instruments[0].filter_table = bytes([0x91, 0x40, 0x83])
    p = assert_6502(swm, NOTE + 3)
    assert playspec.binding(p.view(), p.entry)["kbtrack_fswitch"]
    assert _regs(swm, NOTE + 2)[0x17] & 0x0F == 0x03


def test_a_release_small_fx_leaves_the_attack_decay_a_write_ad_fx_set():
    """SMALFX6 merges into SR only, so BIGFX05's AD value stays."""
    swm = toy_swm()
    swm.patterns[0].rows[1] = Row(fx=0x05, fx_value=0x27)
    swm.patterns[0].rows[2] = Row(fx=0x6C)
    regs = _regs(swm, NOTE + 6)
    assert (regs[5], regs[6] & 0x0F) == (0x27, 0x0C)


def test_an_instrument_at_or_above_the_sel_ins_limit_keeps_the_current_instrument():
    """SEL_INS stores CURINS only below ``cmp #maxinstamount``."""
    swm = toy_swm()
    limit = labels(_run(swm, 1))["sel_ins_limit"]
    swm.patterns[0].rows[1] = Row(note=53, instrument=limit)
    p = assert_6502(swm, NOTE + 4)
    assert (_cell(p, "curins"), _cell(p, "dpitch")) == (1, 53)


@pytest.mark.parametrize("version", ["1.7", "1.97"])
def test_a_jump_to_a_pw_row_restarts_the_sweep_only_where_table_fx_reset(version):
    """BIGFX0A sets PWTPOS; from SID-Maker 1.8 it also zeroes PWEEPCNT, in 1.7 the sweep runs on."""
    swm = toy_swm()
    swm.player_version = version
    swm.instruments[0].pw_table = bytes([0x05, 0x10, 0x00, 0x05, 0x10, 0x00])
    jump = SETUP_FRAMES + 3
    plain = _run(swm, jump + 1)
    swm.patterns[0].rows[1] = Row(fx=0x0A, fx_value=0x01)
    p = assert_6502(swm, jump + 1)
    resets = playspec.binding(p.view(), p.entry)["table_fx_resets"]
    assert resets is (version != "1.7")
    assert _cell(p, "pwtpos") == _PW_ROW_0 + 3
    assert _cell(p, "pweepcnt") == (1 if resets else _cell(plain, "pweepcnt"))


def _staccato_hr() -> SWMFile:
    """A looping WF table, then a fresh note whose instrument's HR control is staccato."""
    swm = _ins(
        toy_swm(), control=0x18, hr_decay=0xF, wf_table=bytes([0x41, 0x80, 0, 0xFE, 0x10, 0])
    )
    swm.instruments.append(dataclasses.replace(swm.instruments[0], control=0x1D))
    return _rows(swm, r2=Row(note=53, instrument=2))


_SHAPES = {
    "slide": _fx_swm(0x02, 0x10),
    "wf_jump_fx": _tables_swm(0x09, 0x01),
    "portamento": _rows(toy_swm(), r3=Row(note=53, instrument=1, fx=0x03, fx_value=0x20)),
    "chord_return": _chord_swm(49, bytes([0x7E])),
    "wf_jump_below_10": _ins(
        _chord_swm(49, bytes([0x7E])),
        wf_table=bytes([0x41, 0, 0, 0xFE, 0x16, 0, 0x05, 0x7F, 0, 0x03, 0, 0]),
    ),
    "wf_jump_abandoned_dc": _ins(toy_swm(), wf_table=bytes([0x41, 0, 0, 0xFE, 0xDC, 0])),
    "wf_jump_abandoned_09": _ins(toy_swm(), wf_table=bytes([0x41, 0, 0, 0xFE, 0x09, 0])),
    "staccato_hr": _staccato_hr(),
    "multispeed_40": _ins(toy_swm(), pw_table=b"\x10\x20\x00", arp_speed=0x40),
    "multispeed_c0": _ins(toy_swm(), pw_table=b"\x10\x20\x00", arp_speed=0xC0),
    "keyboard_tracking": _ins(
        toy_swm(), pw_table=b"\x88\x00\x30", filter_table=bytes([0x91, 0x40, 0x10])
    ),
    "gate_off_filter": _ins(
        toy_swm(), filter_table=bytes([0x91, 0x40, 0, 0x04, 0x08, 0]), gateoff_filt=0x17
    ),
    "legato_small_fx": _rows(toy_swm(), r3=Row(note=50, instrument=0x40)),
    "chord_fx_on_note": _rows(toy_swm(), r3=Row(note=50, fx=0x07, fx_value=0x01)),
    "vibrato_types": _ins(toy_swm(6), control=0x20, vibrato=0x46, vibrato_delay=2),
}


@pytest.mark.parametrize("name", sorted(_SHAPES))
def test_effect_and_table_shapes_play_as_the_6502_does(name):
    """Slide, portamento, chord, WF-jump, HR, multispeed, tracking and vibrato modules."""
    assert_6502(_SHAPES[name], 24)
