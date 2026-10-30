"""Smoke tests for :mod:`pysidwizard.player`."""

from __future__ import annotations

import dataclasses
import warnings

import pytest

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
    SWMUnsupportedEffectWarning,
    Transpose,
    Waveform,
    pack_pattern,
    parse_swm,
    straight_tempo,
)
from pysidwizard.features import features_for_driver
from pysidwizard.player import (
    DEFAULT_HR_ADSR,
    FREQ_TABLE_SPAN,
    FREQ_TABLE_TAIL_LEN,
    FREQTBH_SLOPE,
    FREQTBL_PAST_END,
    HR_FRAMES,
    NOTE_FREQ_HI,
    NOTE_FREQ_LO,
    SWMPlayer,
    VoiceState,
    freq_tables,
)
from tests._variant import with_image

# INITER zeroes SPDCNT, so play calls 0 and 1 are row 0's TICK_0 and TICK_1 and
# its note first applies on call 2 (TICK_2).
SETUP_FRAMES = 2


def _toy_swm() -> SWMFile:
    """One pattern, three sequences, one instrument — enough to exercise
    every per-frame code path.

    Tempo 3 is the shortest period whose TICK_2 the default build reaches
    (see ``SWMPlayer._apply_tick``), so every row applies.
    """
    inst = Instrument(
        name=b"TEST    ",
        attack=0,
        decay=0,
        sustain=0xF,
        release=0,
        first_waveform=Waveform.PULSE | 0x01,  # pulse + gate
        wf_table=bytes([0x41, 0x00, 0xFF]),  # one WF row: pulse+gate, arp +0, no detune
        pw_table=b"\x88\x00\x00",  # set PW to 0x800 then end-of-table
        filter_table=b"",
    )
    pattern = Pattern(
        rows=[
            Row(note=49, instrument=1),  # middle C with instrument
            Row(),  # NOP
            Row(note=0x7E),  # gate-off
            Row(note=53, instrument=1),  # different pitch
            Row(),  # the last row, which the orderlist's End leaves unplayed
        ],
        length=5,
    )
    return SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pattern],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(3), straight_tempo(3))],
    )


def _wf_arp_swm(note: int, arp: int, tail: bytes = b"") -> SWMFile:
    """``_toy_swm`` whose single WF row applies ``arp`` to ``note``."""
    swm = _toy_swm()
    swm.instruments[0].wf_table = bytes([0x41, arp, 0x00, 0xFF])
    swm.patterns[0].rows[0] = Row(note=note, instrument=1)
    return with_image(swm, freq_table_tail=tail)


def _zp_freq(swm: SWMFile) -> tuple:
    v = _play(swm, frames=SETUP_FRAMES + 4).voices[0]
    return v.zp_freq_hi, v.zp_freq_lo


def test_oob_instrument_slot_resolves_to_the_depacker_blank_instrument(oob_sample_bytes: bytes):
    """issue #57: a slot above the count loads the depacker's blank instrument (``dummyihdr``)."""
    swm = parse_swm(oob_sample_bytes)
    p = SWMPlayer(swm)
    # packdepack.inc dummyihdr: control $1A, HR-ADSR $0F, sustain $F, chord 1, wf $09, no tables.
    assert p._empty == Instrument(
        control=0x1A, hr_decay=0xF, sustain=0xF, default_chord=1, first_waveform=0x09
    )
    assert p._resolve_instrument(1) is swm.instruments[0]
    assert p._resolve_instrument(len(swm.instruments) + 1) is p._empty
    assert p._resolve_instrument(0) is None
    assert p._resolve_instrument(0x3F) is None


def test_wf_arp_relative_step_masks_the_note_index_instead_of_clamping():
    """ABSPTCH (player.asm:2525) is ``and #$7F``, so 62 + 126 plays index 60, not 95."""
    assert _zp_freq(_wf_arp_swm(62, 0x7E)) == (NOTE_FREQ_HI[60], NOTE_FREQ_LO[60])
    assert _zp_freq(_wf_arp_swm(62, 0x7E)) != (NOTE_FREQ_HI[95], NOTE_FREQ_LO[95])


def test_wf_arp_step_past_the_last_note_reads_past_the_frequency_tables():
    """``lda FREQTBL,y`` at y=97 leaves the 96-note table; FREQTBH's own past-end is the keyboard-track slope."""
    tail = bytes(range(FREQ_TABLE_TAIL_LEN))
    assert _zp_freq(_wf_arp_swm(49, 0x30, tail)) == (FREQTBH_SLOPE[1], tail[1])


def test_freq_tables_lay_out_the_players_image_order():
    """FREQTBH sits before FREQTBL (player.asm:2958-3005), so reading past it walks the slope, then FREQTBL."""
    lo, hi = freq_tables(features_for_driver(0), b"\xaa\xbb")
    assert (lo[:96], hi[:96]) == (NOTE_FREQ_LO, NOTE_FREQ_HI)
    assert lo[96:98] == b"\xaa\xbb"
    assert hi[96:104] == FREQTBH_SLOPE
    assert hi[104:200] == NOTE_FREQ_LO
    assert (len(lo), len(hi)) == (FREQ_TABLE_SPAN, FREQ_TABLE_SPAN)
    # No image (a bare .swm): the shipped 1.94 player's first past-FREQTBL byte stands in.
    assert set(freq_tables(features_for_driver(0))[0][96:]) == {FREQTBL_PAST_END}


def test_freq_tables_drop_the_slope_when_no_build_flag_assembles_it():
    """The slope is gated on CALCVIBRATO / PWKEYBTRACK / FILTKBTRACK (player.asm:2980)."""
    lo, hi = freq_tables(features_for_driver(2), b"\xaa\xbb")
    assert hi[96:192] == NOTE_FREQ_LO
    assert lo[96:98] == b"\xaa\xbb"


def test_freq_table_has_96_entries_and_matches_middle_c():
    assert len(NOTE_FREQ_LO) == 96
    assert len(NOTE_FREQ_HI) == 96
    word = (NOTE_FREQ_HI[49] << 8) | NOTE_FREQ_LO[49]
    # SID 985,248 Hz / 16,777,216 ~ middle C 261.63 Hz
    hz = word * 985248 / 16777216
    assert 260 < hz < 263


def test_play_frame_returns_offset_diffed_writes():
    # SWMPlayer is a pysidtracker MemPlayer, so play_frame() yields
    # (reg_offset, value) pairs — offsets $00..$18, not absolute $D4xx
    # addresses — and only the registers whose value changed this frame.
    swm = _toy_swm()
    p = SWMPlayer(swm)
    frame0 = p.play_frame()
    # The first frame reports all 25 registers as offset/value pairs.
    assert [r for r, _ in frame0] == list(range(25))
    # Row 0's TICK_0 walks instrument 0 (linked to instrument 1) from table
    # offset 0: header bytes $00 $00 $00 read as a WF row are an ARP-speed
    # override (no waveform write) and relative pitch 0 on DPITCH 0.
    for base in (0, 7, 14):
        assert frame0[base : base + 2] == [(base, NOTE_FREQ_LO[0]), (base + 1, NOTE_FREQ_HI[0])]
        assert frame0[base + 4] == (base + 4, 0x00)
    # Later frames report only changed registers, still as $00..$18 offsets.
    # TICK_1 walks offset 3: AD $00 (ARP-speed) then SR $F0, a relative
    # step of -16 that ABSPTCH masks to note index $70, past FREQTBL.
    lo, hi = freq_tables(features_for_driver(0))
    later = p.play_frame()
    assert later == [
        (reg, val) for base in (0, 7, 14) for reg, val in ((base, lo[0x70]), (base + 1, hi[0x70]))
    ]
    for _ in range(HR_FRAMES + 1):
        p.play_frame()
    # The full forward-filled register state is always live as ``p.regs``:
    # all three voices play the pitched instrument row, so each voice CTRL
    # ($D404/$D40B/$D412) carries pulse+gate and the global volume is max.
    assert len(p.regs) == 25
    for base in (0, 7, 14):
        assert p.regs[base + 4] & 0x41, "voice CTRL has pulse+gate"
    assert p.regs[0x18] & 0x0F == 0x0F


def test_player_advances_notes_over_time():
    swm = _toy_swm()
    p = SWMPlayer(swm)
    freq_hi_values = set()
    for _ in range(64):
        p.play_frame()
        freq_hi_values.add(p.regs[1])  # voice 0 freq hi
    assert len(freq_hi_values) > 1, "expected more than one pitch over time"


def test_player_terminates_when_sequence_ends():
    swm = _toy_swm()
    p = SWMPlayer(swm)
    # Pattern has 4 rows × tempo 2 = 8 frames; after that End() fires and
    # ``finished`` flips on every voice. Give a comfortable margin.
    for _ in range(60):
        p.play_frame()
        if p.finished:
            break
    assert p.finished


def _closing_gate_off(sequence: list, subtunes: int = 1) -> list:
    """Voice 0's control register per frame for a note then a gate-off row,
    tempo 3, every voice playing ``sequence`` (``subtunes`` orderlist sets)."""
    inst = Instrument(name=b"GATE    ", sustain=0xF, first_waveform=0x41, wf_table=b"\x41\x80\xff")
    swm = SWMFile(
        sequences=[sequence] * 3 * subtunes,
        patterns=[Pattern(rows=[Row(note=49, instrument=1), Row(note=0x7E)], length=2)],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(3), straight_tempo(3))] * subtunes,
    )
    return [row[4] for row in SWMPlayer(swm).render_grid(16)]


# The gate-off row applies on its TICK_2: row 1's TICK_0 is call 3.
GATE_OFF_CALL = SETUP_FRAMES + 3


@pytest.mark.parametrize(
    "sequence,subtunes",
    [
        ([PlayPattern(1), End()], 1),
        ([PlayPattern(1), Transpose(2), End()], 1),
        ([PlayPattern(1)], 1),
        ([PlayPattern(1), Loop(position=1)], 1),
        ([PlayPattern(1), SubtuneJump(byte=0x85)], 1),
        ([PlayPattern(1), End(), SubtuneJump(byte=0x81)], 2),
    ],
)
def test_orderlist_end_leaves_the_last_row_unplayed(sequence, subtunes):
    """PTN_SEQ meets the orderlist's end on the tick after the last row's read
    and pins SPDCNT at TICK_1, so that row never reaches TICK_2."""
    ctrl = _closing_gate_off(sequence, subtunes)
    assert all(c & 1 for c in ctrl[GATE_OFF_CALL - 1 :]), ctrl


@pytest.mark.parametrize(
    "sequence,subtunes",
    [
        ([PlayPattern(1), PlayPattern(1), End()], 1),
        ([PlayPattern(1), Loop(position=0)], 1),
        ([PlayPattern(1), SubtuneJump(byte=0x81)], 2),
    ],
)
def test_last_row_plays_when_the_orderlist_goes_on(sequence, subtunes):
    ctrl = _closing_gate_off(sequence, subtunes)
    assert ctrl[GATE_OFF_CALL - 1] & 1 and not ctrl[GATE_OFF_CALL] & 1, ctrl


@pytest.mark.parametrize(
    "arp_byte,base_note,expected_pitch,expected_absolute",
    [
        # SID-Wizard player.asm NORMARP semantics
        # (native/sources/include/player.asm:~1988):
        #   $00..$7E  -> relative pitch UP   (note + arp)
        #   $80       -> NOP (no change)
        #   $81..$DF  -> absolute pitch (arp & 0x7F)
        #   $E0..$FF  -> relative pitch down (arp - 0x100)
        (0x00, 49, 49, False),  # rel-up 0 -> keep base note
        (0x03, 49, 52, False),  # rel-up 3 -> minor third up
        (0x7E, 49, 49 + 0x7E, False),  # max rel-up
        (0x81, 49, 0x01, True),  # absolute pitch 1
        (0xC4, 49, 0x44, True),  # absolute pitch 0x44 (flashitback BASS row)
        (0xDF, 49, 0x5F, True),  # max absolute pitch
        (0xE0, 49, 49 - 0x20, False),  # rel-down -32
        (0xFF, 49, 48, False),  # rel-down -1
    ],
)
def test_wf_arp_byte_interpretation(arp_byte, base_note, expected_pitch, expected_absolute):
    """Pin down the WF-table arp byte semantics against player.asm.

    Constructs a minimal SWM whose voice-0 instrument has a single WF
    row ``[waveform, arp_byte, detune=0]``, runs one frame, then checks
    the voice's ``wf_arp_pitch`` / ``wf_arp_absolute`` ended up where
    the real player would have placed them. ``base_note`` is the
    pattern row's pitch — relevant only as the "before arp" pitch the
    relative variants add to.
    """
    inst = Instrument(
        name=b"ARPTEST ",
        sustain=0xF,
        first_waveform=0x41,
        wf_table=bytes([0x41, arp_byte, 0x00, 0xFF]),
        pw_table=b"",
        filter_table=b"",
    )
    # Pattern is row-0 trigger + several NOP rows so the voice stays on
    # the same note long enough for HR to finish and the WF table to
    # tick at least once.
    pattern = Pattern(
        rows=[Row(note=base_note, instrument=1), Row(), Row(), Row(), Row()],
        length=5,
    )
    swm = SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pattern],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(3), straight_tempo(3))],
    )
    p = SWMPlayer(swm)
    # HR phase runs for HR_FRAMES frames before the WF table ticks
    # for the first time; advance through HR so the arp byte under test
    # actually gets evaluated.
    for _ in range(SETUP_FRAMES + HR_FRAMES + 1):
        p.play_frame()
    v = p.voices[0]
    if expected_absolute:
        assert v.wf_arp_absolute is True
        assert v.wf_arp_pitch == expected_pitch
    else:
        assert v.wf_arp_absolute is False
        # For relative arps, _compose_sid_writes adds wf_arp_pitch to v.note.
        assert v.note + v.wf_arp_pitch == expected_pitch


def test_filter_set_byte_unpacks_band_resonance_cutoff():
    """A filter-table SET-mode row (byte0 >= $80) encodes:
      bits 4-6 = filter band switches (LP/BP/HP -> $D418 mode bits)
      bits 0-3 = resonance        (-> $D417 high nibble)
      next byte = cutoff_hi       (-> $D416; cutoff_lo resets to 0)
    Routing ($D417 low nibble) is composed from voice-active flags.

    Test: voice 0 plays a filtered instrument; voices 1 & 2 play an
    unfiltered instrument. With SET byte $94 (LP filter, resonance 4)
    and cutoff_hi $20, we expect $D416=$20, $D417=$41 (res=$4 + only
    voice 0 routing), $D418=$1F (LP + max vol)."""
    filtered = Instrument(
        name=b"FLTTEST ",
        sustain=0xF,
        first_waveform=0x41,
        wf_table=bytes([0x41, 0x80, 0x00, 0xFF]),
        filter_table=bytes([0x94, 0x20, 0x00, 0xFF]),
    )
    plain = Instrument(
        name=b"PLAIN   ",
        sustain=0xF,
        first_waveform=0x41,
        wf_table=bytes([0x41, 0x80, 0x00, 0xFF]),
        filter_table=b"",  # no filter — voice should not route
    )
    pat_filt = Pattern(rows=[Row(note=49, instrument=1)] + [Row()] * 4, length=5)
    pat_plain = Pattern(rows=[Row(note=49, instrument=2)] + [Row()] * 4, length=5)
    swm = SWMFile(
        sequences=[
            [PlayPattern(1), End()],  # voice 0: filtered
            [PlayPattern(2), End()],  # voice 1: plain
            [PlayPattern(2), End()],  # voice 2: plain
        ],
        patterns=[pat_filt, pat_plain],
        instruments=[filtered, plain],
        subtune_tempos=[(straight_tempo(3), straight_tempo(3))],
    )
    p = SWMPlayer(swm)
    # The filter table only walks during post-HR frames; HR_FRAMES of
    # test-bit playback come first.
    for _ in range(SETUP_FRAMES + HR_FRAMES):
        p.play_frame()
    p.play_frame()
    reg = p.regs
    assert reg[0x15] == 0x00, f"cutoff_lo should reset to 0, got ${reg[0x15]:02X}"
    assert reg[0x16] == 0x20, f"cutoff_hi should be $20, got ${reg[0x16]:02X}"
    assert reg[0x17] == 0x41, f"$D417 should be $41 (res=$4, route=v0), got ${reg[0x17]:02X}"
    assert reg[0x18] == 0x1F, f"$D418 should be $1F (LP + vol $F), got ${reg[0x18]:02X}"


def test_parse_chord_starts_separates_chords_on_7e_and_7f():
    """``$7E`` and ``$7F`` both terminate a chord and start the next one.
    A trailing terminator at the very end of the table doesn't create a
    phantom empty chord. Offsets are into the in-memory CHORDS table
    which has a 1-byte dummy prefix (SWMconvert.c line 2039 sets
    ``ChordIndex[1]=1``), so chord 1 starts at byte 1, not 0."""
    from pysidwizard.player import _parse_chord_starts

    # Three chords: [7,3,0], [7,4,0], [0,3,7,10]. Trailing $7E doesn't
    # create chord 4. In-memory offsets: 1, 5, 9.
    table = bytes([7, 3, 0, 0x7E, 7, 4, 0, 0x7E, 0, 3, 7, 10, 0x7E])
    assert _parse_chord_starts(table) == [1, 5, 9]
    # Mixed $7E / $7F separators (real captures use either).
    table = bytes([0, 3, 7, 0x7F, 0, 5, 7, 0x7F])
    assert _parse_chord_starts(table) == [1, 5]
    # Empty table.
    assert _parse_chord_starts(b"") == []


def test_chord_table_cycles_pitches_with_loop_terminator():
    """A ``$7F`` chord terminator restarts the chord; subsequent $7F
    arp triggers should cycle pitches indefinitely (note + chord[0],
    note + chord[1], ..., wrap)."""
    # Chord 1: [3, 7, 12, $7F] — a major triad loop.
    inst = Instrument(
        name=b"CHORD1  ",
        sustain=0xF,
        first_waveform=0x41,
        default_chord=1,
        # WF table has ONE row that runs the chord every frame.
        # Row: wf=$41, arp=$7F (chord trigger), detune=0, then jump back.
        # Jump targets are absolute offsets within the instrument's
        # memory image; the wf_table itself starts at $10, so "$10"
        # means "jump back to wf_table[0]".
        wf_table=bytes([0x41, 0x7F, 0x00, 0xFE, 0x10]),
    )
    pattern = Pattern(rows=[Row(note=49, instrument=1)] + [Row()] * 12, length=13)
    from pysidwizard import SWMFile, straight_tempo

    swm = SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pattern],
        instruments=[inst],
        chord_table=bytes([3, 7, 12, 0x7F]),
        subtune_tempos=[(straight_tempo(3), straight_tempo(3))],
    )
    p = SWMPlayer(swm)
    for _ in range(SETUP_FRAMES):
        p.play_frame()
    # First note triggers HR for HR_FRAMES frames, then chord cycling
    # begins: post-HR frame -> pitch 3, next -> pitch 7, next -> pitch 12,
    # next -> $7F loops back to pitch 3, and so on.
    pitches = []
    for _ in range(HR_FRAMES + 8):
        p.play_frame()
        pitches.append(p.voices[0].wf_arp_pitch)
    post_hr = pitches[HR_FRAMES:]
    assert post_hr == [3, 7, 12, 3, 7, 12, 3, 7], f"got {post_hr}"


@pytest.mark.parametrize("detune_byte, chord", [(0x10, 3), (0xFF, 3), (0x10, 0x7E)])
def test_chord_row_loads_detune_without_nop_test(detune_byte, chord):
    """PLYCHRD stores a ``$7F`` row's detune byte as is, ``$FF`` included,
    also when the chord returns (``$7E``) to the next WF row."""
    from pysidwizard import SWMFile, straight_tempo

    inst = Instrument(
        name=b"CHORDDT ",
        sustain=0xF,
        first_waveform=0x41,
        default_chord=1,
        wf_table=bytes([0x41, 0x7F, detune_byte, 0x41, 0x00, 0xFF]),
    )
    swm = SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[Pattern(rows=[Row(note=49, instrument=1)] + [Row()] * 12, length=13)],
        instruments=[inst],
        chord_table=bytes([chord, 0x7F]),
        subtune_tempos=[(straight_tempo(3), straight_tempo(3))],
    )
    p = SWMPlayer(swm)
    for _ in range(SETUP_FRAMES + HR_FRAMES + 1):
        p.play_frame()
    v = p.voices[0]
    assert v.detune & 0xFF == detune_byte
    assert v.sid_freq_lo == (v.zp_freq_lo + detune_byte + v.wf_pitch_carry) & 0xFF


def test_chord_table_return_terminator_resets_pos_and_signals_advance():
    """A ``$7E`` chord byte is the 'return from chord to WFARP'
    terminator. The chord-tick method should:
      - return True (so the caller chains to the next WF row)
      - reset chord_pos to the start of the current chord
      - leave wf_arp_pitch unchanged

    Tested by calling ``_tick_chord`` directly with a hand-built state
    so we don't have to construct a WF table that happens to land on
    the right byte at the right frame."""
    inst = Instrument(
        name=b"PLUCK   ",
        sustain=0xF,
        first_waveform=0x41,
        default_chord=1,
        wf_table=bytes([0x41, 0x80, 0x00, 0xFF]),
    )
    pattern = Pattern(rows=[Row(note=49, instrument=1)], length=1)
    from pysidwizard import SWMFile, straight_tempo

    # Chord 1: a single $7E. Chord 2: [5, $7E] (for the chord_start lookup).
    swm = SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pattern],
        instruments=[inst],
        chord_table=bytes([0x7E, 5, 0x7E]),
        subtune_tempos=[(straight_tempo(1), straight_tempo(1))],
    )
    p = SWMPlayer(swm)
    v = p.voices[0]
    # In-memory CHORDS layout (with 1-byte dummy prefix):
    # offset 0=dummy, 1=$7E (chord 1's terminator), 2=$05 (chord 2 first
    # pitch), 3=$7E (chord 2 terminator). Chord 2 starts at offset 2.
    # Seed state as if chord 2 is selected and chord_pos points AT its
    # $7E terminator (offset 3).
    v.instrument = inst
    v.current_chord = 2
    v.chord_pos = 3  # pointing AT the $7E byte of chord 2
    v.wf_arp_pitch = 99  # sentinel — must NOT be modified by $7E
    result = p._tick_chord(v)
    assert result is True, "expected $7E to signal 'advance WF row'"
    assert v.chord_pos == 2, "expected chord_pos reset to chord 2's start"
    assert v.wf_arp_pitch == 99, "expected wf_arp_pitch unchanged on $7E"


def test_hard_restart_holds_test_bit_for_hr_frames():
    """During the HR phase a triggered voice must emit CTRL=
    ``first_waveform AND ptn_gate`` (the instrument's first-frame
    waveform with the pattern gate mask applied), the post-HR AD
    (the player pre-loads it; AD doesn't matter while the test bit
    silences the oscillator), and the HR-SR slot — for exactly
    HR_FRAMES frames before transitioning to normal play and letting
    the WF-table waveform through.

    HR fires on EVERY note trigger including a voice's first one (the
    reference captures for flashitback show CTRL=$09 on the song's first
    note for v0 and v2 — see [[flashitback-dump-findings]]). We sample the
    first note's TICK_2 to verify."""
    inst = Instrument(
        name=b"HRTEST  ",
        control=0x08,  # bit 3 = test-bit HR enabled (FRAME1SWITCH gate)
        hr_attack=0,
        hr_decay=0xF,
        hr_sustain=0,
        hr_release=0,  # HR-SR=$00
        attack=1,
        decay=2,
        sustain=0xA,
        release=0xA,  # post-HR AD=$12, SR=$AA
        first_waveform=0x09,  # SID-Wizard default: test bit + gate
        wf_table=bytes([0x41, 0x80, 0x00, 0xFF]),  # pulse + gate post-HR
    )
    pattern = Pattern(
        rows=[Row(note=49, instrument=1)] + [Row()] * 4,
        length=5,
    )
    swm = SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pattern],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(3), straight_tempo(3))],
    )
    p = SWMPlayer(swm)
    samples = []
    for _ in range(SETUP_FRAMES):
        p.play_frame()
    for _ in range(HR_FRAMES + 2):
        p.play_frame()
        v0 = p.voices[0]
        samples.append((v0.sid_ctrl, v0.sid_ad, v0.sid_sr))
    # HR frames (samples 0..HR_FRAMES-1): CTRL is first_waveform ($09)
    # AND'd with ptn_gate ($FF after _start_note) = $09. AD/SR are the
    # *post-HR* values — the HR-SR value lives in the 2-frames-before
    # pre-write, not the HR frame itself (see _maybe_emit_pre_hr).
    for f in range(HR_FRAMES):
        ctrl, ad, sr = samples[f]
        assert ctrl == 0x09, f"HR frame {f}: expected CTRL=$09 (test+gate), got ${ctrl:02X}"
        assert ad == 0x12, f"HR frame {f}: expected AD=$12 (post-HR), got ${ad:02X}"
        assert sr == 0xAA, f"HR frame {f}: expected SR=$AA (post-HR), got ${sr:02X}"
    # Post-HR frame: CTRL gets the WF-table waveform, AD/SR are unchanged.
    ctrl, ad, sr = samples[HR_FRAMES]
    assert ad == 0x12, f"post-HR AD: expected $12, got ${ad:02X}"
    assert sr == 0xAA, f"post-HR SR: expected $AA, got ${sr:02X}"
    assert ctrl & 0x40, f"expected pulse waveform from wf_table, got ${ctrl:02X}"
    assert not (ctrl & 0x08), f"test bit should be off post-HR, got ${ctrl:02X}"


def test_first_note_runs_hard_restart():
    """A voice's first-ever note DOES run the HR test-bit phase —
    flashitback ref CSV 21 shows v0 and v2 emitting CTRL=$09 on the
    song's row-0 trigger. With first_waveform=$09 (the SID-Wizard
    default) and ptn_gate=$FF post-_start_note, the HR CTRL is $09.

    The TICK_0 / TICK_1 setup frames before it walk instrument 1's header
    as WF rows: control byte $08 is below $10, an ARP-speed override that
    writes no waveform and then holds the walk, so CTRL stays $00.
    """
    inst = Instrument(
        name=b"FIRST   ",
        control=0x08,  # bit 3 = test-bit HR enabled
        sustain=0xF,
        first_waveform=0x09,  # test bit + gate
        wf_table=bytes([0x41, 0x80, 0x00, 0xFF]),  # pulse + gate post-HR
    )
    pattern = Pattern(rows=[Row(note=49, instrument=1)] + [Row()] * 4, length=5)
    swm = SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pattern],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(3), straight_tempo(3))],
    )
    p = SWMPlayer(swm)
    for f in range(SETUP_FRAMES):
        p.play_frame()
        assert p.voices[0].sid_ctrl == 0x00, f"setup frame {f}"
    p.play_frame()
    assert (
        p.voices[0].sid_ctrl == 0x09
    ), f"first note must run HR; got CTRL=${p.voices[0].sid_ctrl:02X}"


def test_hard_restart_freezes_wf_table_position():
    """A 1-row WF table tick advances ``wf_pos`` by ``WF_ROW_STRIDE``;
    that advance must NOT happen during the HR phase, otherwise the
    table runs past its first row before the test bit is released.

    HR only fires on note-to-note transitions, so the test pattern has
    a warm-up note (which skips HR) followed by a fresh note that does
    trigger HR — we sample wf_pos during the HR window of that second
    trigger."""
    from pysidwizard.constants import INST_WF_TABLE_POS
    from pysidwizard.player import WF_ROW_STRIDE

    inst = Instrument(
        name=b"HRWF    ",
        sustain=0xF,
        first_waveform=0x41,
        wf_table=bytes([0x41, 0x80, 0x00, 0x21, 0x80, 0x00, 0xFF]),
    )
    pattern = Pattern(
        rows=[Row(note=49, instrument=1), Row(note=53, instrument=1)] + [Row()] * 6,
        length=8,
    )
    swm = SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pattern],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(4), straight_tempo(4))],
    )
    p = SWMPlayer(swm)
    # Tempo=4 -> row 0 applies on frame SETUP_FRAMES, row 1 four frames later.
    # Run through row 0 first (warm-up, no HR, WF table advances).
    for _ in range(SETUP_FRAMES + 4):
        p.play_frame()
    # Row 1's note triggers _start_note, which resets wf_pos to the
    # WF table's base offset AND starts the HR window. During HR wf_pos must
    # stay put (the WF table is paused).
    for f in range(HR_FRAMES):
        p.play_frame()
        assert (
            p.voices[0].wf_pos == INST_WF_TABLE_POS
        ), f"HR frame {f}: WF table ticked (wf_pos={p.voices[0].wf_pos})"
    # On the first post-HR frame the WF table finally ticks.
    p.play_frame()
    assert p.voices[0].wf_pos == INST_WF_TABLE_POS + WF_ROW_STRIDE


def test_untriggered_voice_emits_no_register_writes():
    """A voice whose first pattern row carries no note and no instrument
    must produce zero writes to its $D40x register block — SID-Wizard's
    player gates the per-voice write loop on having seen a real note.

    Voice 0 plays a note; voices 1 and 2 sit on empty rows, so their
    register blocks stay at zero on every frame.
    """
    inst = Instrument(
        name=b"V0      ",
        sustain=0xF,
        first_waveform=0x41,
        wf_table=bytes([0x41, 0x80, 0x00, 0xFF]),
    )
    pattern_active = Pattern(rows=[Row(note=49, instrument=1), Row()], length=2)
    pattern_silent = Pattern(rows=[Row()], length=1)  # note=None instrument=None
    swm = SWMFile(
        sequences=[
            [PlayPattern(1), End()],  # voice 0: note + instrument
            [PlayPattern(2), End()],  # voice 1: empty row
            [PlayPattern(2), End()],  # voice 2: empty row
        ],
        patterns=[pattern_active, pattern_silent],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(3), straight_tempo(3))],
    )
    # Step past the setup and HR test-bit frames into a steady CNTPLY2
    # frame. A voice that never triggered leaves its register block at the
    # reset baseline (the memory model forward-fills untouched registers).
    grid = SWMPlayer(swm).render_grid(SETUP_FRAMES + HR_FRAMES + 1)
    for regs in grid:
        assert regs[7:21] == [0] * 14, "voices 1 and 2 (empty rows) must stay silent"
    regs = grid[-1]
    # Voice 0 (triggered) is programmed: pulse+gate CTRL and a nonzero pitch.
    assert regs[4] & 0x41, "voice 0 CTRL must carry pulse+gate"
    assert regs[0] or regs[1], "voice 0 FREQ must be programmed"
    # Voices 1 and 2 sit on empty rows and were never triggered, so their
    # register blocks ($D407.., $D40E..) stay silent.
    assert regs[7:14] == [0] * 7, "voice 1 (empty row) must stay silent"
    assert regs[14:21] == [0] * 7, "voice 2 (empty row) must stay silent"


def test_voice_starts_emitting_once_triggered():
    """A voice that's empty on row 0 but plays a note on row 1 must
    start emitting writes on the frame the note triggers — and keep
    emitting thereafter, even when later rows are empty."""
    inst = Instrument(
        name=b"LATE    ",
        sustain=0xF,
        first_waveform=0x41,
        wf_table=bytes([0x41, 0x80, 0x00, 0xFF]),
    )
    # Voice 0: row 0 empty, row 1 plays a note, row 2 empty.
    pattern = Pattern(
        rows=[Row(), Row(note=49, instrument=1), Row()],
        length=3,
    )
    swm = SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pattern],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(1), straight_tempo(1))],
    )
    p = SWMPlayer(swm)
    frames_with_v0 = []
    for _ in range(4):
        p.play_frame()
        frames_with_v0.append(p.voices[0].triggered)
    # Frame 0: row 0 is empty -> not triggered. Frame 1+: note triggers.
    assert frames_with_v0[0] is False
    assert all(frames_with_v0[1:]), f"expected v0 active from frame 1 on: {frames_with_v0}"


def test_player_handles_loop_command_without_infinite_advance():
    """A Loop(position=0) re-enters the sequence at the start each time
    an End-like terminator is reached — but since SID-Wizard's player
    treats Loop as the actual terminator, the player should never get
    "stuck" in the sequence-advance loop within a single frame."""
    pattern = Pattern(rows=[Row(note=49, instrument=1)], length=1)
    inst = Instrument(name=b"LP      ", sustain=0xF, first_waveform=0x41)
    from pysidwizard import Loop

    swm = SWMFile(
        sequences=[[PlayPattern(1), Loop(position=0)]] * 3,
        patterns=[pattern],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(1), straight_tempo(1))],
    )
    p = SWMPlayer(swm)
    for _ in range(40):
        p.play_frame()
    # The loop variant never sets ``finished``; the player must still
    # have produced steady writes.
    assert not p.finished


def test_subtune_jump_terminator_continues_in_target_subtune():
    """A $FF terminator followed by a high-bit byte is a per-channel jump to
    another subtune (player.asm LOOPSEQ line 1683 -> SETSEQA, then position 0),
    not a loop position and not the end of playback."""

    inst = Instrument(name=b"SJ      ", sustain=0xF, first_waveform=0x41)
    swm = SWMFile(
        sequences=(
            [[PlayPattern(1), SubtuneJump(byte=0x81)]] * 3 + [[PlayPattern(2), Loop(0)]] * 3
        ),
        patterns=[
            Pattern(rows=[Row(note=49, instrument=1)], length=1),
            Pattern(rows=[Row(note=61, instrument=1)], length=1),
        ],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(4), straight_tempo(4))] * 2,
    )
    p = SWMPlayer(swm)
    for _ in range(SETUP_FRAMES):
        p.play_frame()
    notes = set()
    for _ in range(40):
        p.play_frame()
        notes.add(p.voices[0].note)
    assert not p.finished, "subtune jump must not end playback"
    assert notes == {49, 61}, f"expected both subtunes' notes to play, got {notes}"
    assert p.voices[0].sequence is swm.sequences[3]
    assert p.voices[0].pattern is swm.patterns[1]


def test_orderlist_main_volume_applies_delayed_at_pattern_boundary():
    """The orderlist MainVolume seq-fx ($A0..$AF) sets the low nibble of
    $D418 with the SEQVOLU->MAINVOL one-frame delay: the change lands when
    the following pattern's first row is read, not while the previous
    pattern is still playing."""
    inst = Instrument(name=b"VOL     ", sustain=0xF, first_waveform=0x41)
    pat1 = Pattern(rows=[Row(note=49, instrument=1), Row()], length=2)
    pat2 = Pattern(rows=[Row(note=53, instrument=1), Row()], length=2)
    swm = SWMFile(
        sequences=[
            [PlayPattern(1), MainVolume(5), PlayPattern(2), End()],
            [PlayPattern(1), PlayPattern(2), End()],
            [PlayPattern(1), PlayPattern(2), End()],
        ],
        patterns=[pat1, pat2],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(2), straight_tempo(2))],
    )
    p = SWMPlayer(swm)
    v0 = p.voices[0]
    change_frame = None
    boundary_frame = None
    for frame in range(12):
        was_pat1 = v0.pattern is pat1
        p.play_frame()
        if was_pat1 and v0.pattern is pat2 and boundary_frame is None:
            boundary_frame = frame
        if change_frame is None and (p.regs[0x18] & 0x0F) == 5:
            change_frame = frame
    # Volume reached 5 exactly at the pattern-1 -> pattern-2 boundary read,
    # never leaking into pattern 1, and stayed there.
    assert boundary_frame is not None
    assert change_frame == boundary_frame
    assert p.regs[0x18] & 0x0F == 5


def test_orderlist_main_volume_absent_keeps_default_volume():
    """Without a MainVolume seq-fx the master volume stays at $F."""
    inst = Instrument(name=b"VOL     ", sustain=0xF, first_waveform=0x41)
    pat = Pattern(rows=[Row(note=49, instrument=1), Row()], length=2)
    swm = SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pat],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(2), straight_tempo(2))],
    )
    p = SWMPlayer(swm)
    for _ in range(8):
        p.play_frame()
        assert p.regs[0x18] & 0x0F == 0x0F


def _hr_timer_swm(control: int) -> SWMFile:
    """Tempo-3 tune: a held note, then a fresh note two rows later whose
    instrument's ``control`` byte selects the hard-restart tick. The WF
    table loops (``$FE $10``) so every frame re-writes the waveform byte
    through ``ptn_gate``, making the gate mask observable in CTRL."""
    inst = Instrument(
        name=b"HRTIME  ",
        control=control,
        hr_attack=0,
        hr_decay=0xF,
        hr_sustain=0,
        hr_release=0,
        sustain=0xF,
        first_waveform=0x41,
        wf_table=bytes([0x41, 0x80, 0x00, 0xFE, 0x10, 0x00]),
    )
    pattern = Pattern(
        rows=[Row(note=49, instrument=1), Row(), Row(note=53, instrument=1), Row()],
        length=4,
    )
    return SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pattern],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(3), straight_tempo(3))],
    )


@pytest.mark.parametrize(
    "control,gate_off_tick0,gate_off_tick1",
    [
        # ISHARDR (player.asm:1564) ANDs the instrument control byte with
        # A=$02 at TICK_0 (HARDRST, line 1518) / A=$01 at TICK_1 (HRDR1FR,
        # line 1726) and takes ``beq HRENDER`` when the bit is clear, so
        # HRGTOFF's PTNGATE=$FE (lines 1568-1571) runs on the firing tick
        # only.
        (0x1A, True, True),  # bit 1: fires at TICK_0, mask persists to TICK_1
        (0x1D, False, True),  # bit 0: fires at TICK_1 only (staccato exit)
        (0x18, False, False),  # neither bit: no HR, gate held to STRTSND
    ],
)
def test_pre_hr_gate_off_only_on_firing_tick(control, gate_off_tick0, gate_off_tick1):
    """The pre-HR gate mask must not drop a frame early: an instrument
    whose HR fires at TICK_1 has to keep the previous note gated through
    TICK_0, otherwise its tail is cut by one frame."""
    p = SWMPlayer(_hr_timer_swm(control))
    frames = []
    tick0 = SETUP_FRAMES + 4
    for _ in range(tick0 + 2):
        p.play_frame()
        frames.append((p.voices[0].sid_ctrl, p.voices[0].ptn_gate))
    # Tempo 3, note on row 2: row 2's TICK_0 / TICK_1 follow row 1's TICK_2.
    for frame, gate_off in ((tick0, gate_off_tick0), (tick0 + 1, gate_off_tick1)):
        ctrl, ptn_gate = frames[frame]
        want = "off" if gate_off else "on"
        assert (
            not ctrl & 0x01
        ) is gate_off, (
            f"control=${control:02X} frame {frame}: gate {want} expected, CTRL=${ctrl:02X}"
        )
        assert (
            ptn_gate == 0xFE
        ) is gate_off, (
            f"control=${control:02X} frame {frame}: mask {want} expected, PTNGATE=${ptn_gate:02X}"
        )


def _fx_swm(fx: int, fx_value=None) -> SWMFile:
    """``_toy_swm`` with the given effect column on the first (note) row."""
    swm = _toy_swm()
    swm.patterns[0].rows[0] = Row(note=49, instrument=1, fx=fx, fx_value=fx_value)
    return swm


def _play(swm: SWMFile, frames: int = 12) -> SWMPlayer:
    p = SWMPlayer(swm)
    for _ in range(frames):
        p.play_frame()
    return p


def test_bare_instrument_select_walks_the_instrument_header():
    """A table cursor the player has not reset yet reads the instrument HEADER.

    player.asm INIPVAR (line 641) zeroes WFTPOS / PWTPOS / FLTPOS, and a bare
    instrument select takes COLUMN1's ``jsr INSPTFX ; jmp CNTPLY2`` path
    (line 1777) without reaching STRTSND, so the cursors stay at 0 — an
    absolute offset from the instrument base, i.e. header byte 0.
    """
    inst = Instrument(
        name=b"STALE   ",
        control=0x0A,
        hr_decay=0x0F,
        hr_sustain=0x0F,
        sustain=0xF,
        first_waveform=0x41,
        wf_table=bytes([0x41, 0x80, 0x00, 0xFF]),
    )
    swm = SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[Pattern(rows=[Row(instrument=1)] + [Row()] * 7, length=8)],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(8), straight_tempo(8))],
    )
    setup = SWMPlayer(swm).render_grid(SETUP_FRAMES + 10)
    grid = setup[SETUP_FRAMES:]
    # Row 0 carries no note, so its TICK_0 / TICK_1 take NONEWNO with CURINS
    # still 0: no table walk and only WFGHOST ($00) is written.
    assert [row[0:7] for row in setup[:SETUP_FRAMES]] == [[0] * 7] * SETUP_FRAMES
    # PWSWEEP (player.asm:2334) reads header byte 0 ($0A) as the sweep-cycle
    # count and header byte 1 (HR-AD, $0F) as the per-frame PW-low delta.
    assert [row[0x02] for row in grid] == [(0x0F * (f + 1)) & 0xFF for f in range(10)]
    # RDWFROW (player.asm:2404) reads the same header bytes: $0A is below the
    # $10 waveform threshold so it is an ARP-speed override and NO waveform is
    # written, while $0F is a relative pitch. Walking wf_table row 0 instead
    # would write its $41 waveform and its $80 NOP arp (leaving pitch at 0).
    assert [row[0x04] for row in grid] == [0x00] * 10
    freqs = {(row[0x00], row[0x01]) for row in grid}
    assert len(freqs) == 1 and freqs != {(0x00, 0x00)}, freqs


def test_unmodelled_big_fx_warns_and_is_tallied():
    """An effect the player does not implement must be observable, not swallowed.

    ``$15`` (BIGFX15 track tempo-program, player.asm:3684) is unmodelled.
    """
    with pytest.warns(SWMUnsupportedEffectWarning) as record:
        p = _play(_fx_swm(0x15, 0x04))
    assert p.unsupported_effects == {("big-fx", 0x15): 3}, "once per voice, all three voices"
    warning = record[0].message
    assert (warning.column, warning.code, warning.voice) == ("big-fx", 0x15, 2)
    assert "$15" in str(warning)


def test_unmodelled_fx_warns_once_per_code_but_tallies_every_hit():
    """BIGFX12 (main tempo-program, player.asm:3642) is unmodelled."""
    swm = _fx_swm(0x12, 0x02)
    swm.patterns[0].rows[1] = Row(fx=0x12, fx_value=0x02)
    with pytest.warns(SWMUnsupportedEffectWarning) as record:
        p = _play(swm)
    assert p.unsupported_effects[("big-fx", 0x12)] == 6, "2 rows x 3 voices applied"
    assert len(record) == 1, "warned once per (column, code), tallied per application"


def test_unsupported_effect_warning_can_be_promoted_to_an_error():
    with pytest.raises(SWMUnsupportedEffectWarning):
        with warnings.catch_warnings():
            warnings.simplefilter("error", SWMUnsupportedEffectWarning)
            _play(_fx_swm(0x15, 0x04))


def test_modelled_effects_do_not_warn():
    """BIGFX03/$08/$10 and the $Ax / $2x small-FX stay silent."""
    for fx, fx_value in ((0x03, 0x20), (0x08, 0x42), (0x10, 0x03), (0xA7, None), (0x2C, None)):
        with warnings.catch_warnings():
            warnings.simplefilter("error", SWMUnsupportedEffectWarning)
            _play(_fx_swm(fx, fx_value))


def test_small_fx_a_sets_main_volume_nibble():
    """SMALFXA (player.asm:3356) writes MAINVOL + SEQVOLU, so $D418 tracks it."""
    p = _play(_fx_swm(0xA7))
    assert p.regs[0x18] & 0x0F == 0x07
    assert p.seqvolu == 0x07


def test_big_fx_04_writes_the_waveform_register():
    """BIGFX04 = WRITEWF (player.asm:3509) stores the value straight into WFGHOST.

    INSPTFX runs after STRTSND (player.asm:2054), so on a note row the effect's
    waveform wins over the instrument's first_waveform ($41 here).
    """
    assert _play(_fx_swm(0x02, 0x00), frames=SETUP_FRAMES + 1).regs[4] == 0x00, "STRTSND baseline"
    assert _play(_fx_swm(0x04, 0x21), frames=SETUP_FRAMES + 1).regs[4] == 0x21


def test_big_fx_05_writes_the_attack_decay_register():
    """BIGFX05 = WRITEAD (player.asm:3512) writes $D405 directly (ALLGHOSTREGS_ON=0)."""
    assert _play(_fx_swm(0x02, 0x00), frames=SETUP_FRAMES + 1).regs[5] == 0x00, "STRTSND baseline"
    assert _play(_fx_swm(0x05, 0xA3), frames=SETUP_FRAMES + 1).regs[5] == 0xA3


def test_big_fx_06_writes_the_sustain_release_register():
    """BIGFX06 = WRITESR (player.asm:3514) writes $D406 directly (ALLGHOSTREGS_ON=0)."""
    assert _play(_fx_swm(0x02, 0x00), frames=SETUP_FRAMES + 1).regs[6] == 0xF0, "STRTSND baseline"
    assert _play(_fx_swm(0x06, 0x5C), frames=SETUP_FRAMES + 1).regs[6] == 0x5C


def test_big_fx_register_writes_apply_on_a_note_less_row():
    """PATT_FX also runs on rows with no note column (player.asm:1778)."""
    swm = _fx_swm(0x04, 0x21)
    swm.patterns[0].rows[1] = Row(fx=0x06, fx_value=0x39)
    p = _play(swm, frames=SETUP_FRAMES + 4)
    assert p.regs[6] == 0x39
    assert p.regs[4] == 0x41, "the WF table walk owns the waveform again by this frame"


def test_small_fx_4_sets_the_waveform_high_nibble():
    """SMALFX4 (player.asm:3298) merges the value nibble over WFGHOST's high nibble.

    ``lda WFGHOST,x ; jsr SETNIBH`` (player.asm:3216) keeps the low nibble --
    gate/sync/ring/test bits survive the waveform change.
    """
    swm = _fx_swm(0x02, 0x00)
    swm.patterns[0].rows[1] = Row(fx=0x48)
    p = _play(swm, frames=SETUP_FRAMES + 4)
    assert _play(_fx_swm(0x02, 0x00), frames=SETUP_FRAMES + 4).regs[4] == 0x41, "WF table baseline"
    assert p.regs[4] == 0x81, "waveform nibble replaced, gate bit kept"


def test_small_fx_e_is_a_no_op_in_1_94():
    """SMALFXE (player.asm:3407) computes a merge but its ``jmp WRITEWF`` is
    commented out, so the register is untouched -- and it must not report as
    unmodelled."""
    swm = _fx_swm(0x02, 0x00)
    swm.patterns[0].rows[1] = Row(fx=0xE0)
    with warnings.catch_warnings():
        warnings.simplefilter("error", SWMUnsupportedEffectWarning)
        p = _play(swm, frames=SETUP_FRAMES + 4)
    assert p.regs[4] == 0x41, "WFGHOST unchanged: the store is commented out upstream"


def test_big_fx_0f_sets_the_cutoff_high_byte():
    """BIGFX0F (player.asm:3563) writes CTFHGHO, the $D416 ghost."""
    assert _play(_fx_swm(0x02, 0x00), frames=SETUP_FRAMES + 1).regs[0x16] == 0x00
    assert _play(_fx_swm(0x0F, 0xC4), frames=SETUP_FRAMES + 1).regs[0x16] == 0xC4


def test_big_fx_1f_sets_filter_switches_and_resonance():
    """BIGFX1F (player.asm:3758) stores the value's nibbles into FSWITCH and RESONIB.

    Both are plain ``sta``s, so it also clears the per-voice routing bits until
    the next STRTSND re-ORs them (player.asm:2001).
    """
    p = _play(_fx_swm(0x1F, 0x38), frames=SETUP_FRAMES + 1)
    assert p.regs[0x17] == 0x38
    assert p.filter_switch == 0x08 and p.filter_resonance == 0x30


def test_small_fx_b_sets_the_filter_band_nibble():
    """SMALFXB (player.asm:3377) writes FLTBAND, the $D418 high nibble."""
    p = _play(_fx_swm(0xB2), frames=SETUP_FRAMES + 1)
    assert p.regs[0x18] == 0x2F, "band $2 over the default $F main volume"


def test_small_fx_f_sets_the_resonance_nibble():
    """SMALFXF (player.asm:3418) writes RESONIB, the $D417 high nibble."""
    p = _play(_fx_swm(0xF9), frames=SETUP_FRAMES + 1)
    assert p.regs[0x17] == 0x90


def test_filter_routing_bit_follows_the_instrument_filter_table():
    """FSWITCH's routing bits are owned by STRTSND's SETFLTP (player.asm:1999-2008)."""
    swm = _toy_swm()
    swm.instruments[0].filter_table = b"\x80\x40\x00\xff"
    p = _play(swm, frames=SETUP_FRAMES + 1)
    assert p.filter_switch & 0x07 == 0x07, "all three voices play the filtered instrument"
    assert p.regs[0x17] & 0x0F == 0x07


def _tables_swm(fx: int, fx_value: int) -> SWMFile:
    """``_fx_swm`` whose instrument has multi-row WF / PW tables and two chords."""
    swm = _fx_swm(fx, fx_value)
    ins = swm.instruments[0]
    ins.wf_table = bytes([0x41, 0x80, 0x00, 0x11, 0x80, 0x00, 0xFF])
    ins.pw_table = bytes([0x88, 0x00, 0x00, 0x84, 0x00, 0x00, 0xFF])
    ins.arp_speed = 0x02
    swm.chord_table = bytes([0x00, 0x04, 0x07, 0x7F, 0x00, 0x03, 0x07, 0x7F])
    return swm


def test_big_fx_09_jumps_to_a_waveform_table_row():
    """BIGFX09 (player.asm:3524): WFTPOS := value*3 + WFTABLEPOS."""
    from pysidwizard.constants import INST_WF_TABLE_POS

    p = _play(_tables_swm(0x09, 0x01), frames=SETUP_FRAMES + 1)
    assert p.voices[0].wf_pos == INST_WF_TABLE_POS + 3


def test_big_fx_0a_jumps_to_a_pulse_table_row_and_clears_the_sweep():
    """BIGFX0A (player.asm:3530): PWTPOS := value*3 + instrument byte $0A, PWEEPCNT := 0."""
    swm = _tables_swm(0x0A, 0x01)
    base = 0x10 + len(swm.instruments[0].wf_table) + 1
    p = _play(swm, frames=SETUP_FRAMES + 1)
    assert p.voices[0].pw_pos == base + 3
    assert p.voices[0].pw_sweep_count == 0


def test_big_fx_0c_and_small_fx_c_set_the_arpeggio_speed():
    """BIGFX0C = SMALFXC (player.asm:3391): ARPSPED := value, ARPSCNT := $FF."""
    p = _play(_tables_swm(0x0C, 0x05), frames=SETUP_FRAMES + 1)
    assert (p.voices[0].arp_speed_reload, p.voices[0].arp_speed_counter) == (0x05, 0xFF)
    p = _play(_tables_swm(0xC3, 0x00), frames=SETUP_FRAMES + 1)
    assert (p.voices[0].arp_speed_reload, p.voices[0].arp_speed_counter) == (0x03, 0xFF)


def test_big_fx_07_selects_a_chord_by_full_byte():
    """BIGFX07 = SMALFX7 (player.asm:3325): CURCHORD := value, CHORDPOS := CHDPTRLO[value]."""
    p = _play(_tables_swm(0x07, 0x02), frames=SETUP_FRAMES + 1)
    assert p.voices[0].current_chord == 2
    assert p.voices[0].chord_pos == p._chord_starts[1]


def test_big_fx_0d_and_small_fx_d_detune_the_voice():
    """BIGFX0D = SETDETU (player.asm:3551) stores DETUNER; SMALFXD scales by 8."""
    assert _play(_fx_swm(0x0D, 0x05), frames=SETUP_FRAMES + 1).voices[0].detune == 5
    assert _play(_fx_swm(0x0D, 0xFB), frames=SETUP_FRAMES + 1).voices[0].detune == -5
    assert _play(_fx_swm(0xD2), frames=SETUP_FRAMES + 1).voices[0].detune == 16


def test_big_fx_0e_sets_the_pulse_width_high_nibble():
    """BIGFX0E (player.asm:3558): PWHIGHO := value & $0F."""
    assert _play(_fx_swm(0x0E, 0x3A), frames=SETUP_FRAMES + 1).voices[0].pw_hi == 0x0A


def test_big_fx_16_forces_the_vibrato_type():
    """BIGFX16 = FORCVI2 (player.asm:3698): SLIDEVIB := value & $30."""
    assert _play(_fx_swm(0x16, 0x37), frames=SETUP_FRAMES + 1).voices[0].slide_vib == 0x30


def test_small_fx_9_sets_the_vibrato_frequency_only():
    """SMALFX9 (player.asm:3349): VIBFREQU := nibble * 2 (``asl``)."""
    assert _play(_fx_swm(0x93), frames=SETUP_FRAMES + 1).voices[0].vibrato_period == 6


def test_small_fx_8_overrides_the_vibrato_amplitude_nibble():
    """SMALFX8 = VIBAMFX (player.asm:3338) keeps the instrument's frequency nibble."""
    swm = _fx_swm(0x84)
    swm.instruments[0].vibrato = 0x25
    v = _play(swm, frames=SETUP_FRAMES + 1).voices[0]
    assert v.vibrato_period == 10, "frequency nibble $5 comes from the instrument"
    assert (v.vibrato_freqmod_lo, v.vibrato_freqmod_hi) != (0, 0), "amplitude $4 applied"


def test_big_fx_08_with_zero_amplitude_clears_freqmod():
    """SETFMOD's ``beq wrFmodL`` (player.asm:2924) zeroes FREQMOD for amplitude 0."""
    swm = _fx_swm(0x08, 0x07)
    swm.instruments[0].vibrato = 0x84
    v = _play(swm, frames=SETUP_FRAMES + 1).voices[0]
    assert (v.vibrato_freqmod_lo, v.vibrato_freqmod_hi) == (0, 0)
    assert v.vibrato_period == 14


def test_exptabh_layout_matches_the_assembled_player():
    """EXPTABH (player.asm:2958-2982) is 11 zeros, FREQTBH, then an 8-byte slope."""
    from pysidwizard.player import EXP_MAX_INDEX, EXP_THRESHOLD, EXPTABH

    assert EXPTABH[:11] == bytes(11)
    assert EXPTABH[11:107] == NOTE_FREQ_HI
    assert EXPTABH[107:] == bytes([0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFE, 0xFF, 0xFF])
    assert (EXP_THRESHOLD, EXP_MAX_INDEX) == (107, 203)


@pytest.mark.parametrize(
    "index,expected",
    [
        # LOOKUPA (player.asm:2925): below EXPTRESHOLD the fine half is EXPTABH[y]
        # with a zero high byte; at or above it, the FREQTBL/FREQTBH pair at
        # y - EXPTRESHOLD; MAXSLID clamps y to 203.
        (0, (0x00, 0x00)),
        (10, (0x00, 0x00)),
        (11, (NOTE_FREQ_HI[0], 0x00)),
        (106, (NOTE_FREQ_HI[95], 0x00)),
        (107, (NOTE_FREQ_LO[0], NOTE_FREQ_HI[0])),
        (202, (NOTE_FREQ_LO[95], NOTE_FREQ_HI[95])),
        (203, (0xC9, 0xF9)),
        (255, (0xC9, 0xF9)),
    ],
)
def test_lookup_freqmod_walks_the_exponent_table_exactly(index, expected):
    assert SWMPlayer(_toy_swm())._lookup_freqmod(index) == expected


@pytest.mark.parametrize("calcvibrato", [True, False])
def test_freqmod_reads_the_images_own_freqtbh(calcvibrato):
    """EXPTABH,Y and FREQTBH-1,Y index the image, so a retuned FREQTBH retunes FREQMOD."""
    swm = _toy_swm()
    lo, hi = freq_tables(features_for_driver(0))
    retuned = bytes((b + 1) & 0xFF for b in hi[:96]) + hi[96:]
    with_image(swm, freq_lo=lo, freq_hi=retuned)
    p = SWMPlayer(swm)
    p.features = dataclasses.replace(p.features, calcvibrato=calcvibrato)
    assert p.exptabh[11:] == retuned
    if calcvibrato:
        assert p._lookup_freqmod(39) == (retuned[28], 0)
        assert p._freqmod(0x20, 28) == (retuned[33], 0)
    else:
        assert p._freqmod(5, 0) == (retuned[4], 0)
    assert SWMPlayer(_toy_swm())._lookup_freqmod(39) == (NOTE_FREQ_HI[28], 0)


@pytest.mark.parametrize("fx,reg", [(0x05, 5), (0x06, 6)])
def test_write_adsr_reaches_the_sid_on_a_voice_with_no_instrument(fx, reg):
    """CURINS=0 sends TICK_2 to LEGATOO, whose INSPTFX writes SIDG.AD/SR (player.asm:1326)."""
    swm = _toy_swm()
    swm.patterns[0].rows[0] = Row(note=0x7E, fx=fx, fx_value=0x9C)
    p = _play(swm, frames=SETUP_FRAMES + 1)
    assert p.voices[0].instrument is None
    assert p.regs[reg] == 0x9C
    assert p.regs[11 - reg] == 0, "only the written half of the ADSR pair changes"


def test_write_sr_reaches_the_sid_on_a_legato_row():
    """A legato row's LEGATOO runs INSPTFX, so WRITESR lands with only CTRL beside it."""
    swm = _toy_swm()
    swm.patterns[0].rows[3] = Row(note=53, instrument=0x3F, fx=0x06, fx_value=0x5A)
    p = SWMPlayer(swm)
    legato = []
    for _ in range(20):
        p.play_frame()
        if p.voices[0].in_tick2_legato_this_frame:
            legato.append(p.regs[6])
    assert legato == [0x5A]


@pytest.mark.parametrize("fx", [0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C])
def test_big_fx_1c_shifts_the_cutoff_and_17_to_1b_alias_it(fx):
    """BIGFX1C (player.asm:3706) sets FLSHIFT, which $D416 adds to the cutoff ghost.

    BIGFX17..BIGFX1B (player.asm:3700-3704) are bare labels immediately ahead of
    BIGFX1C, so all six BIGFXTABLE entries resolve to the same routine.
    """
    swm = _fx_swm(0x0F, 0x40)
    swm.patterns[0].rows[1] = Row(fx=fx, fx_value=0x10)
    swm.patterns[0].rows[2] = Row(fx=fx, fx_value=0xF0)
    assert _play(swm, frames=SETUP_FRAMES + 1).regs[0x16] == 0x40
    assert _play(swm, frames=SETUP_FRAMES + 4).regs[0x16] == 0x50
    assert _play(swm, frames=SETUP_FRAMES + 7).regs[0x16] == 0x30, "$80..$FF shifts down"


def _tempo_of(p: SWMPlayer, idx: int) -> tuple:
    v = p.voices[idx]
    return (v.tempo_left, v.tempo_right)


def test_big_fx_10_and_11_set_the_main_tempo_on_every_track():
    """MAINTMP (player.asm:3591) writes TEMPOTBL+0 and rewinds every track's TMPPOS.

    BIGFX10 (player.asm:3586) ``ora #$80``s the value, pinning the funk cycle to
    the one slot; BIGFX11 (player.asm:3627) splits the byte into two slots.
    """
    p = _play(_fx_swm(0x10, 0x04), frames=SETUP_FRAMES + 1)
    assert [_tempo_of(p, i) for i in range(3)] == [(4, 4)] * 3
    p = _play(_fx_swm(0x11, 0x53), frames=SETUP_FRAMES + 1)
    assert [_tempo_of(p, i) for i in range(3)] == [(5, 3)] * 3


def test_big_fx_13_and_14_set_the_tempo_of_one_track_only():
    """BIGFX13 / BIGFX14 (player.asm:3658 / 3669) write the track's own tempo slot."""
    swm = _fx_swm(0x13, 0x04)
    swm.sequences = [[PlayPattern(1), End()], [PlayPattern(2), End()], [PlayPattern(2), End()]]
    swm.patterns.append(Pattern(rows=[Row(note=49, instrument=1)] + [Row()] * 3, length=4))
    p = _play(swm, frames=SETUP_FRAMES + 1)
    assert _tempo_of(p, 0) == (4, 4)
    assert _tempo_of(p, 1) == _tempo_of(p, 2) == (3, 3), "other tracks keep the subtune tempo"


def test_big_fx_03_without_a_note_rearms_the_slide():
    """BIGFX03 (player.asm:3505) is SETSLID + SETFMOD, note column or not.

    With no new note, DPITCH is unchanged, so the slide simply continues toward
    the note already playing at the newly given speed.
    """
    swm = _fx_swm(0x02, 0x10)
    swm.patterns[0].rows[1] = Row(fx=0x03, fx_value=0x20)
    with warnings.catch_warnings():
        warnings.simplefilter("error", SWMUnsupportedEffectWarning)
        v = _play(swm, frames=SETUP_FRAMES + 4).voices[0]
    assert v.slide_vib == 0x83
    assert (v.vibrato_freqmod_lo, v.vibrato_freqmod_hi) == SWMPlayer(swm)._lookup_freqmod(
        0x10 + 49
    ), "SETFMOD index is ceil(value/2) + DPITCH"


def test_delay_fx_are_no_ops_on_the_default_driver_but_report_on_extra():
    """DELAYSUPPORT_ON is set only by the Extra player (altplayers.inc:553).

    In every other build BIGFX1D / BIGFX1E (player.asm:3730 / 3746) compile down
    to a bare ``rts``, so they are no-ops rather than unmodelled effects.
    """
    swm = _fx_swm(0x1E, 0x04)
    with warnings.catch_warnings():
        warnings.simplefilter("error", SWMUnsupportedEffectWarning)
        assert _play(swm, frames=SETUP_FRAMES + 4).unsupported_effects == {}
    swm.driver_type = 3
    with pytest.warns(SWMUnsupportedEffectWarning):
        assert _play(swm, frames=SETUP_FRAMES + 4).unsupported_effects == {("big-fx", 0x1E): 3}


def _extra(swm: SWMFile) -> SWMFile:
    """Mark a module as exported for the Extra build (driver type 3)."""
    swm.driver_type = 3
    return swm


@pytest.mark.parametrize("tempo,expected_apply_tick", [(1, 0), (2, 1), (3, 2), (6, 2)])
def test_fastspeedbind_collapses_the_row_ticks_at_tempo_1_and_2(tempo, expected_apply_tick):
    """FASTSPEEDBIND_ON short-circuits CHKTMP1/CHKTMP2 (player.asm:1496, 1704).

    Below tempo 2 TICK_0 falls straight through to PTN_SEQ and TICK_2; below
    tempo 3 TICK_1 does. Without the flag TICK_2 only ever runs at SPDCNT 2.
    """
    default = SWMPlayer(_toy_swm())
    extra = SWMPlayer(_extra(_toy_swm()))
    assert default._apply_tick(tempo) == 2
    assert extra._apply_tick(tempo) == expected_apply_tick


@pytest.mark.parametrize("tempo", [1, 2])
def test_extra_build_advances_rows_at_tempo_1_and_2(tempo):
    """The default build reaches no TICK_2 at tempo 1-2, so no note ever applies.

    Without FASTSPEEDBIND SPDCNT wraps before reaching 2, so no voice ever runs
    STRTSND and PTNGATE keeps INITER's $00, masking every CTRL write to $00.
    The Extra build applies each row at call ``tempo - 1`` of the row (TICK_0
    at tempo 1, TICK_1 at tempo 2), so ``_toy_swm`` row 3's pitch first
    reaches $D401 on the frame after its HR frame.
    """
    swm = _toy_swm()
    swm.subtune_tempos = [(straight_tempo(tempo), straight_tempo(tempo))]
    default = SWMPlayer(swm).render_grid(16)
    assert {row[ctrl] for row in default for ctrl in (4, 11, 18)} == {0x00}, "silent"
    extra = SWMPlayer(_extra(swm)).render_grid(16)
    first = next(f for f, row in enumerate(extra) if row[1] == NOTE_FREQ_HI[53])
    assert first == 3 * tempo + (tempo - 1) + HR_FRAMES


def test_allghostregs_combines_two_adsr_small_fx_in_one_row():
    """SMALFX2/3/5/6 read SIDG.AD/SR under ALLGHOSTREGS_ON (player.asm:3278-3315).

    Without it each takes the untouched nibble from the instrument, so a $5x
    sustain on one row is wiped by a $6x release on the next; with it they merge.
    """
    swm = _toy_swm()
    swm.patterns[0].rows[1] = Row(instrument=0x50)
    swm.patterns[0].rows[2] = Row(instrument=0x69)
    assert _play(swm, frames=SETUP_FRAMES + 7).regs[6] == 0xF9, "instrument sustain $F wins back"
    assert _play(_extra(swm), frames=SETUP_FRAMES + 7).regs[6] == 0x09, "the $50 sustain survives"


def _medium(swm: SWMFile) -> SWMFile:
    """Mark a module as exported for the Medium build (driver type 1)."""
    swm.driver_type = 1
    return swm


def test_hardrestypes_off_uses_the_default_hr_adsr_on_both_pre_hr_ticks():
    """defauHR (player.asm:1588) writes DEFAULTHRADSR = $0F00 for every instrument.

    Without HARDRESTYPES_ON, HARDRST has no per-instrument control-bit test and
    falls straight into HRGTOFF, so HR fires on TICK_0 and TICK_1 alike.
    """
    swm = _toy_swm()
    swm.subtune_tempos = [(straight_tempo(6), straight_tempo(6))]
    swm.instruments[0].hr_attack = 0xA
    swm.instruments[0].hr_decay = 0xB
    swm.instruments[0].hr_sustain = 0xC
    swm.instruments[0].hr_release = 0xD
    swm.instruments[0].control = 0x1A
    default = _play(swm, frames=SETUP_FRAMES + 17)
    assert (default.regs[5], default.regs[6]) == (0xAB, 0xCD), "per-instrument HR ADSR"
    medium = _play(_medium(swm), frames=SETUP_FRAMES + 17)
    assert (medium.regs[5], medium.regs[6]) == (DEFAULT_HR_ADSR >> 8, DEFAULT_HR_ADSR & 0xFF)


def test_frame1switch_off_always_starts_the_note_on_waveform_09():
    """STRTSND drops the ``and #8`` test and does ``lda #$09`` (player.asm:1849)."""
    swm = _toy_swm()
    swm.instruments[0].control = 0x12  # bit 3 clear: the default build writes no CTRL
    swm.instruments[0].first_waveform = 0x89
    assert _play(swm, frames=SETUP_FRAMES + 1).regs[4] != 0x09
    assert _play(_medium(swm), frames=SETUP_FRAMES + 1).regs[4] == 0x09


def test_filt_ctrl_fx_off_makes_big_fx_1f_a_bare_rts():
    """Without FILT_CTRL_FX_ON the BIGFX1F body is gone and RTBIGFX's rts takes over."""
    swm = _fx_swm(0x1F, 0x35)
    assert _play(swm, frames=SETUP_FRAMES + 4).regs[0x17] == 0x35
    with warnings.catch_warnings():
        warnings.simplefilter("error", SWMUnsupportedEffectWarning)
        assert _play(_medium(swm), frames=SETUP_FRAMES + 4).regs[0x17] != 0x35


def _light(swm: SWMFile) -> SWMFile:
    """Mark a module as exported for the Light build (driver type 2)."""
    swm.driver_type = 2
    return swm


def test_octaveshift_off_ignores_the_instruments_octave_field():
    """CLEGATO's ``ldy #9 ; adc (PLAYERZP),y`` is gone (player.asm:1787)."""
    swm = _toy_swm()
    swm.instruments[0].octave_shift = 12
    assert _play(swm, frames=SETUP_FRAMES + 2).regs[1] == NOTE_FREQ_HI[49 + 12]
    assert _play(_light(swm), frames=SETUP_FRAMES + 2).regs[1] == NOTE_FREQ_HI[49]


def test_detunesupport_off_drops_the_detune_add_and_its_carry():
    """WRPITCH loses both ``adc DETUNER,x`` and ``adc #0`` (player.asm:2540-2560)."""
    swm = _fx_swm(0x0D, 0x40)
    assert _play(swm, frames=SETUP_FRAMES + 4).regs[0] == (NOTE_FREQ_LO[49] + 0x40 + 1) & 0xFF
    assert _play(_light(swm), frames=SETUP_FRAMES + 4).regs[0] == NOTE_FREQ_LO[49]


def test_finefiltsweep_off_makes_the_filter_sweep_an_8_bit_add():
    """Without the flag CTFL_GHO does not exist, so the delta lands straight on $D416."""
    swm = _toy_swm()
    swm.instruments[0].filter_table = bytes([0x91, 0x40, 0x00, 0x04, 0x08, 0x00, 0xFF])
    fine = _play(swm, frames=SETUP_FRAMES + 6)
    coarse = _play(_light(swm), frames=SETUP_FRAMES + 6)
    assert coarse.regs[0x16] > fine.regs[0x16], "8-bit sweep steps 8x faster than 11-bit"


def test_calcvibrato_off_makes_the_vibrato_amplitude_pitch_independent():
    """EXPTBASE becomes FREQTBH-1 and SETFMOD drops ``lsr ; adc DPITCH`` (player.asm:2883)."""
    low = SWMPlayer(_light(_toy_swm()))
    assert low._freqmod(0x40, 20) == low._freqmod(0x40, 90)
    calc = SWMPlayer(_toy_swm())
    assert calc._freqmod(0x40, 20) != calc._freqmod(0x40, 90)


def _set_offset(p, v, offset):
    """FREQLO/FREQHI ``offset`` away from FREQTBL[DPITCH]."""
    word = (p._dpitch_freq(v.dpitch) + offset) & 0xFFFF  # pylint: disable=protected-access
    v.zp_freq_lo, v.zp_freq_hi = word & 0xFF, word >> 8


def _offset(p, v):
    """FREQLO/FREQHI minus FREQTBL[DPITCH], 16-bit."""
    return (
        ((v.zp_freq_hi << 8) | v.zp_freq_lo) - p._dpitch_freq(v.dpitch)
    ) & 0xFFFF  # pylint: disable=protected-access


@pytest.mark.parametrize("portavibra,offset", [(False, -0x400 + 0x21), (True, 0)])
def test_a_pending_note_fx_portamento_slides_unless_portavibra(portavibra, offset):
    """SLIDES (player.asm:2091-2098): a note-FX portamento's SLIDEVIB=$FF runs PORTAME
    each frame, or TARGETN with PORTAVIBRA_ON."""
    p = _play(_fx_swm(0x02, 0x10), frames=SETUP_FRAMES + 1)
    p.features = dataclasses.replace(p.features, portavibra=portavibra)
    v = p.voices[0]
    v.slide_vib = 0xFF
    _set_offset(p, v, -0x400)
    v.vibrato_freqmod_lo, v.vibrato_freqmod_hi = 0x20, 0
    p._tick_vibrato(v)  # pylint: disable=protected-access
    assert _offset(p, v) == offset & 0xFFFF and v.slide_vib == 0xFF


@pytest.mark.parametrize(
    "portavibra,signed,offset",
    [
        (False, -0x20, 1),
        (True, -0x20, 0),
        (False, 0x20, 0),
        (True, 0x20, -1),
        (False, 0x8000, 0x7FDF),
    ],
)
def test_portame_tests_the_difference_with_the_carry_slides_leaves(portavibra, signed, offset):
    """PORTAME's ``sbc FREQLO`` (player.asm:2101) borrows unless ``cpy #$82`` set the carry;
    PORTAVIBRA_ON's ``cpy #$FF`` clears it, moving both snap boundaries one unit. The
    16-bit compare is unsigned, so a pitch $8000 above the target still slides down."""
    p = _play(_fx_swm(0x02, 0x10), frames=SETUP_FRAMES + 1)
    p.features = dataclasses.replace(p.features, portavibra=portavibra)
    v = p.voices[0]
    v.slide_vib = 0x83
    _set_offset(p, v, signed)
    v.vibrato_freqmod_lo, v.vibrato_freqmod_hi = 0x20, 0
    p._tick_vibrato(v)  # pylint: disable=protected-access
    assert _offset(p, v) == offset & 0xFFFF


@pytest.mark.parametrize("portavibra", [False, True])
def test_portend_restores_the_instrument_vibrato_only_with_portavibra(portavibra):
    """PORTEND's ``jsr FORCVIB ; jsr SETVIB1`` (player.asm:2118) ends the $83 slide."""
    swm = _fx_swm(0x02, 0x10)
    swm.instruments[0].control = 0x10
    swm.instruments[0].vibrato = 0x46
    p = _play(swm, frames=SETUP_FRAMES + 1)
    p.features = dataclasses.replace(p.features, portavibra=portavibra)
    v = p.voices[0]
    v.slide_vib, v.vibrato_period = 0x83, 0
    _set_offset(p, v, 1)
    v.vibrato_freqmod_lo, v.vibrato_freqmod_hi = 0x20, 0
    p._tick_vibrato(v)  # pylint: disable=protected-access
    assert _offset(p, v) == 0
    assert (v.slide_vib, v.vibrato_period) == ((0x10, 12) if portavibra else (0x83, 0))


@pytest.mark.parametrize("target,carry", [(0xDC, True), (0x09, False)])
def test_an_abandoned_wf_jump_keeps_the_jump_compare_carry(target, carry):
    """WFAJUMP's ``BMIauto ENDWFTB`` (player.asm:2422) reaches WRPITCH with ``cmp #$FE``'s C=1."""
    from pysidwizard.constants import INST_WF_TABLE_POS

    swm = _toy_swm()
    swm.instruments[0].wf_table = bytes([0x41, 0x00, 0x00, 0xFE, target, 0x00])
    p = _play(swm, frames=SETUP_FRAMES + 1)
    v = p.voices[0]
    v.wf_pos, v.arp_speed_counter = INST_WF_TABLE_POS + 3, 0
    p._tick_wf_table(v)  # pylint: disable=protected-access
    assert v.wf_pitch_carry is carry


def test_the_slot_0_instrument_is_the_one_curins_0_walks():
    """HRENDER loads PLAYERZP from INSPTLO/HI slot 0 (player.asm:1594), which an image may point away from instrument 1."""
    blank = Instrument(control=0x1A, hr_decay=0xF, sustain=0xF, first_waveform=0x09)
    swm = _toy_swm()
    swm.patterns[0].rows[0] = Row(note=49)
    linked = SWMPlayer(swm).render_grid(1)[0]
    with_image(swm, dummy_instrument=blank)
    own = SWMPlayer(swm).render_grid(1)[0]
    # WFTPOS is 0, so RDWFROW reads header bytes 0..2: control, then HR-AD as a relative arp.
    assert (linked[0], linked[1]) == (NOTE_FREQ_LO[0], NOTE_FREQ_HI[0])
    assert (own[0], own[1]) == (NOTE_FREQ_LO[0x0F], NOTE_FREQ_HI[0x0F])
    assert swm.subtune(0).player.dummy_instrument is blank


def test_a_track_tempo_set_at_tick_2_rows_from_the_next_frame():
    """The frame-start ``sbc TEMPOTBL`` (player.asm:1356) already sees the TICK_2 fx's tempo."""
    swm = _fx_swm(0x13, 0x03)
    swm.subtune_tempos = [(straight_tempo(6), straight_tempo(6))]
    p = _play(swm, frames=SETUP_FRAMES + 1)
    assert (p.voices[0].tempo_left, p.voices[0].pattern_row) == (3, 1)
    p.play_frame()
    assert p.voices[0].pattern_row == 2, "SPDCNT=3 meets the new $83 slot: TICK_0 reads row 1"


@pytest.mark.parametrize(
    "left_end,toggle,tempo,counter,match,next_toggle",
    [
        (False, 0, 4, 4, True, 1),  # funk slot reached exactly: step to the right slot
        (False, 0, 4, 5, False, 0),  # funk slot passed: no zero, no overflow
        (False, 1, 2, 3, True, 0),  # bit-7 right slot passed: overflow rewinds to TMPBASE
        (True, 0, 4, 5, True, 0),  # single tempo passed
        (True, 0, 4, 3, False, 0),
    ],
)
def test_tempo_match_is_the_sbc_zero_or_overflow(
    left_end, toggle, tempo, counter, match, next_toggle
):
    v = VoiceState(channel=0, tempo_left_end=left_end, tempo_toggle=toggle, speed_counter=counter)
    assert SWMPlayer._tempo_match(v, tempo) is match  # pylint: disable=protected-access
    assert v.tempo_toggle == next_toggle
    assert v.speed_counter == (0 if match else counter)


# Toy PW table base: header, three WF-table bytes and their terminator.
_PW = 0x10 + 4


@pytest.mark.parametrize(
    "pw_table,pos,pw_lo,count,carry,after",
    [
        (b"\x10\x20\x00", _PW, 0xF0, 0, True, (0x09, 0x10, _PW)),  # PWSWEEP's adc carries
        (b"\x10\x20\x00", _PW, 0x00, 0, False, (0x08, 0x20, _PW)),
        (b"\x10\x20\x00", _PW, 0x00, 0x10, True, (0x08, 0x00, _PW + 3)),  # cmp PWEEPCNT equal
        (b"\x88\x00\x00", _PW, 0x00, 0, False, (0x08, 0x00, _PW + 3)),  # SETPULW via bcc
        (b"", _PW, 0x00, 0, True, (0x08, 0x00, _PW)),  # $FF end
        (b"\xfe" + bytes([_PW]) + b"\x00", _PW, 0x00, 0, True, (0x08, 0x00, _PW)),  # self-jump
        (b"\x88\x40\x00\xfe" + bytes([_PW]) + b"\x00", _PW + 3, 0, 0, False, (0x08, 0x40, _PW + 3)),
        (b"\xfe" + bytes([_PW + 3]) + b"\x00\x10\x20\x00", _PW, 0, 0, True, (0x08, 0x00, _PW + 3)),
    ],
)
def test_a_skipwft_frame_adds_the_carry_setpwid_leaves(pw_table, pos, pw_lo, count, carry, after):
    """SKIPWFT's ``jmp ENDWFTB`` (player.asm:2409) reaches WRPITCH's ``adc DETUNER`` with SETPWID's carry."""
    swm = _toy_swm()
    swm.instruments[0].pw_table = pw_table
    p = _play(swm, frames=SETUP_FRAMES + 1)
    p.features = dataclasses.replace(p.features, pwkeybtrack=False)
    v = p.voices[0]
    v.pw_hi, v.pw_lo, v.pw_pos, v.pw_sweep_count = 0x08, pw_lo, pos, count
    v.skipwft_this_tick, v.wf_pitch_carry = True, not carry
    p._setpwid(v)  # pylint: disable=protected-access
    assert v.wf_pitch_carry is carry
    assert (v.pw_hi, v.pw_lo, v.pw_pos) == after
    v.skipwft_this_tick, v.wf_pitch_carry = False, not carry
    p._setpwid(v)  # pylint: disable=protected-access
    assert v.wf_pitch_carry is (not carry), "a read WF row sets its own carry"


@pytest.mark.parametrize(
    "kbd_track,pw_hi,carry", [(0, 0xFF, False), (0x10, 0xFF, True), (0x10, 0x00, False)]
)
def test_pwkeybtrack_carry_is_endpwtbs_adc(kbd_track, pw_hi, carry):
    """ENDPWTB's ``clc`` then ``adc PWHIGHO`` (player.asm:2377-2386) sets the carry under PWKEYBTRACK."""
    swm = _toy_swm()
    swm.instruments[0].pw_table = b""
    p = _play(swm, frames=SETUP_FRAMES + 1)
    p.features = dataclasses.replace(p.features, pwkeybtrack=True)
    v = p.voices[0]
    v.pw_hi, v.pw_kbd_track, v.pw_pos = pw_hi, kbd_track, _PW
    v.skipwft_this_tick = True
    p._setpwid(v)  # pylint: disable=protected-access
    assert v.pw_carry and v.wf_pitch_carry is carry


@pytest.mark.parametrize("arp_speed", [0x40, 0x80, 0xC0])
def test_multispeed_walks_the_pw_table_for_either_flag(arp_speed):
    """MULTIFI's ``jmp FILTPRG`` falls through into SETPWID (player.asm:1321-1327)."""
    swm = _toy_swm()
    swm.instruments[0].pw_table = b"\x10\x20\x00"
    swm.instruments[0].arp_speed = arp_speed
    p = _play(swm, frames=SETUP_FRAMES + 2)
    v = p.voices[0]
    before = v.pw_lo
    p._mulcntp(v)  # pylint: disable=protected-access
    assert v.pw_lo == (before + 0x20) & 0xFF


@pytest.mark.parametrize(
    "slide_vib,vibratotypes", [(0x10, True), (0x20, True), (0x30, True), (0x00, False)]
)
def test_normvib_runs_for_every_positive_slidevib(slide_vib, vibratotypes):
    """``ldy SLIDEVIB,x ; beq INCVIBR ; bpl NORMVIB`` (player.asm:2086-2089); INCVIBR needs VIBRATOTYPES."""
    p = _play(_toy_swm(), frames=SETUP_FRAMES + 1)
    p.features = dataclasses.replace(p.features, vibratotypes=vibratotypes)
    v = p.voices[0]
    v.slide_vib, v.vibrato_delay_counter = slide_vib, -1
    _set_offset(p, v, 0)
    v.vibrato_freq_counter, v.vibrato_period = 4, 8
    v.vibrato_freqmod_lo, v.vibrato_freqmod_hi = 2, 0
    p._tick_vibrato(v)  # pylint: disable=protected-access
    assert (_offset(p, v), v.vibrato_freqmod_lo) == (2, 2)


@pytest.mark.parametrize(
    "vibratotypes,control,counter",
    [(False, 0x30, 3), (True, 0x10, 3), (True, 0x20, 6), (True, 0x30, 0)],
)
def test_setvibr_starts_vibracnt_by_vibrato_type(vibratotypes, control, counter):
    """SETVIBR (player.asm:2894-2911): a quarter period, or with VIBRATOTYPES half for $20 and none for $30."""
    swm = _toy_swm()
    swm.instruments[0].control = control
    swm.instruments[0].vibrato = 0x46
    p = _play(swm, frames=SETUP_FRAMES + 1)
    p.features = dataclasses.replace(p.features, vibratotypes=vibratotypes)
    v = p.voices[0]
    p._setup_vibrato(v)  # pylint: disable=protected-access
    assert (v.slide_vib, v.vibrato_period, v.vibrato_freq_counter) == (control, 12, counter)


def test_a_vibrato_delay_with_bit_7_starts_at_once():
    """SETVIB0 stores the delay byte whole and NORMVIB's ``bmi DOVIBRA`` tests it (player.asm:2890, 2105)."""
    swm = _toy_swm()
    swm.instruments[0].vibrato_delay = 0x80
    p = _play(swm, frames=SETUP_FRAMES + 1)
    assert p.voices[0].vibrato_delay_counter < 0


def _curins0_swm(row: Row) -> SWMFile:
    """``_toy_swm`` whose voice 1 plays ``row`` with no instrument ever selected."""
    swm = _toy_swm()
    swm.patterns.append(Pattern(rows=[row, Row(), Row()], length=3))
    swm.sequences = [[PlayPattern(1), End()], [PlayPattern(2), End()], [PlayPattern(2), End()]]
    return swm


@pytest.mark.parametrize("retain,sr", [(False, 0xFF), (True, 0x0F)])
def test_curins_0_small_fx_reads_the_playerzp_another_track_left(retain, sr):
    """With CURINS=0 TICK_2 jumps to LEGATOO (player.asm:1752-1754): SETINBL reads PLAYERZP as left.

    Voice 0 runs last each frame, and its TICK_1 HRENDER loads instrument 1
    (sustain $F, release 0); voice 1's SMALFX6 merges its release nibble into
    that SR byte next frame unless PLAYER restores the caller's zeropage.
    """
    p = SWMPlayer(_curins0_swm(Row(fx=0x6F)))
    p.features = dataclasses.replace(p.features, retainzeropage=retain)
    grid = p.render_grid(SETUP_FRAMES + 1)
    assert grid[SETUP_FRAMES][0x0D] == sr


@pytest.mark.parametrize(
    "code,ad,sr",
    [(0x2C, 0xC7, None), (0x3C, 0x5C, None), (0x5C, None, 0xC1), (0x6C, None, 0x9C)],
)
def test_curins_0_adsr_small_fx_merges_the_playerzp_bytes(code, ad, sr):
    """SMALFX2/3/5/6 read the other nibble through SETINBH / SETINBL (player.asm:3214-3223)."""
    p = SWMPlayer(_toy_swm())
    zp = Instrument(attack=5, decay=7, sustain=9, release=1)
    p._zp = p._image(zp)  # pylint: disable=protected-access
    v = VoiceState(channel=1)
    p._apply_small_fx(v, code)  # pylint: disable=protected-access
    assert v.adsr_emit_required
    assert (v.sid_ad if ad is not None else v.sid_sr) == (ad if ad is not None else sr)


def test_curins_0_vibrato_fx_reads_the_playerzp_instrument():
    """VIBAMFX's SETINBH (Y=5) and FORCVIB's control byte come through PLAYERZP.

    player.asm:3338-3346.
    """
    p = SWMPlayer(_toy_swm())
    p._zp = p._image(Instrument(control=0x20, vibrato=0x03))  # pylint: disable=protected-access
    v = VoiceState(channel=1)
    p._apply_small_fx(v, 0x84)  # pylint: disable=protected-access
    assert (v.slide_vib, v.vibrato_period) == (0x20, 6)


def test_tick_0_points_playerzp_at_the_packed_pattern():
    """TICK_0 / TICK_1 load PLAYERZP from PPTRLO/HI (player.asm:1388, 1628)."""
    swm = _curins0_swm(Row(fx=0x6F))
    swm.sequences[0] = [PlayPattern(2), End()]
    swm.patterns[1].rows[2] = Row(note=0x7E, fx=0x2A)
    p = SWMPlayer(swm)
    p.features = dataclasses.replace(p.features, retainzeropage=False)
    p.play_frame()
    packed = pack_pattern(swm.patterns[1].encode_unpacked())
    assert p._zp == packed + b"\xff"  # pylint: disable=protected-access
    assert p.render_grid(SETUP_FRAMES)[-1][0x06] == (packed[4] & 0xF0) | 0x0F == 0xFF


def test_a_sid_pattern_image_reads_past_its_terminator():
    """A ``.sid`` pattern's PLAYERZP window carries the image bytes after its $FF."""
    swm = _toy_swm()
    pattern = swm.patterns[0]
    packed = pack_pattern(pattern.encode_unpacked()) + b"\xff"
    pattern.ram = packed + b"\x05\x99"
    assert SWMPlayer(swm)._pattern_image(pattern) == pattern.ram  # pylint: disable=protected-access
    pattern.ram = b"\x00" + packed
    assert SWMPlayer(swm)._pattern_image(pattern) == packed  # pylint: disable=protected-access


def test_a_staccato_hr_leaves_playerzp_at_the_upcoming_instrument():
    """HRGTOFF's staccato ``rts`` (player.asm:1586) precedes HRENDER's reload of CURINS."""
    swm = _hr_timer_swm(0x18)
    swm.instruments.append(dataclasses.replace(swm.instruments[0], control=0x1D))
    swm.patterns[0].rows[2] = Row(note=53, instrument=2)
    p = _play(swm, frames=SETUP_FRAMES + 6)
    assert p._zp is p._image(swm.instruments[1])  # pylint: disable=protected-access


@pytest.mark.parametrize(
    "instrument, fx, hardrestypes, carry",
    [
        (None, None, True, False),
        (None, 0x02, True, False),
        (None, 0x07, True, True),
        (None, 0x07, False, False),
        (0x40, None, True, True),
        (0x01, 0x07, True, False),
    ],
)
def test_hardrst_leaves_its_last_compare_carry_for_a_skipwft_wrpitch(
    instrument, fx, hardrestypes, carry
):
    """NEWNOTE's ``cpy #$03`` or ``cpy #$3F`` (player.asm:1527-1537) is the carry WRPITCH adds."""
    p = _play(_toy_swm(), frames=SETUP_FRAMES + 1)
    p.features = dataclasses.replace(p.features, hardrestypes=hardrestypes)
    v = p.voices[0]
    v.hr_timer, v.arp_speed_counter, v.wf_pitch_carry = 0, 5, not carry
    v.pending_row = Row(note=50, instrument=instrument, fx=fx)
    p._tick_note_setup(v, 0, 2)  # pylint: disable=protected-access
    assert v.skipwft_this_tick
    assert v.wf_pitch_carry is carry


def _gate_off_swm(**pointers) -> SWMFile:
    swm = _toy_swm()
    ins = swm.instruments[0]
    ins.wf_table = bytes([0x41, 0x00, 0x00, 0xFE, 0x10, 0x00, 0x21, 0x00, 0x00, 0xFE, 0x16, 0x00])
    ins.pw_table = bytes([0x88, 0x00, 0x00, 0x84, 0x00, 0x00])
    for name, value in pointers.items():
        setattr(ins, name, value)
    return swm


def test_a_gate_off_wf_pointer_jumps_the_wf_table_and_keeps_the_gate():
    """NGATEOF's ``bne SETGOFF`` (player.asm:3121) moves WFTPOS instead of clearing PTNGATE."""
    gated = _play(_gate_off_swm(gateoff_wf=0x16), frames=SETUP_FRAMES + 9)
    plain = _play(_gate_off_swm(), frames=SETUP_FRAMES + 9)
    assert gated.voices[0].ptn_gate == 0xFF
    assert gated.regs[4] == 0x21
    assert plain.voices[0].ptn_gate == 0xFE
    assert plain.regs[4] == 0x40


def test_gate_off_pw_and_filter_pointers_follow_the_wf_one():
    """chPWgof / chFlTrk (player.asm:3127-3173) run whether or not the WF pointer was set."""
    p = _play(_gate_off_swm(gateoff_pw=0x1A, gateoff_filt=0x30), frames=SETUP_FRAMES + 1)
    v = p.voices[0]
    p.filter_controller_voice, p.filt_pos = v, 0x20
    p._ngateof(v)  # pylint: disable=protected-access
    assert (v.ptn_gate, v.pw_pos, p.filt_pos) == (0xFE, 0x1A, 0x30)
    p.filter_controller_voice = p.voices[1]
    p._ngateof(v)  # pylint: disable=protected-access
    assert p.filt_pos == 0x30
    p.features = dataclasses.replace(p.features, gateoffptr=False)
    v.pw_pos = 0x13
    p._ngateof(v)  # pylint: disable=protected-access
    assert v.pw_pos == 0x13


def _chord_swm(
    note: int, chord: bytes, wf_table: bytes = bytes([0x41, 0x7F, 0x00, 0xFF])
) -> SWMFile:
    """``_toy_swm`` whose instrument plays chord 1 (``chord``) through ``wf_table``."""
    swm = _toy_swm()
    swm.instruments[0].wf_table = wf_table
    swm.instruments[0].default_chord = 1
    swm.chord_table = chord
    swm.patterns[0].rows[0] = Row(note=note, instrument=1)
    return swm


@pytest.mark.parametrize(
    "swm,carry",
    [
        (_chord_swm(49, bytes([0xFB, 0x7F])), True),
        (_chord_swm(49, bytes([0x03, 0x7F])), False),
        (
            dataclasses.replace(
                _wf_arp_swm(5, 0x0C), sequences=[[Transpose(-16), PlayPattern(1), End()]] * 3
            ),
            True,
        ),
        (_wf_arp_swm(5, 0x0C), False),
    ],
)
def test_relptch_carries_any_arp_plus_dpitch_past_ff(swm, carry):
    """RELPTCH's ``clc ; adc DPITCH`` carry reaches WRPITCH's ``adc DETUNER`` (player.asm:2035-2050)."""
    v = _play(swm, frames=SETUP_FRAMES + HR_FRAMES + 1).voices[0]
    assert v.wf_pitch_carry is carry
    assert v.sid_freq_lo == (v.zp_freq_lo + carry) & 0xFF


def test_a_bare_instrument_select_keeps_the_held_dpitch():
    """CLEGATO latches DPITCH at the note (player.asm:1786); the next note takes the new octave shift."""
    swm = _toy_swm()
    swm.instruments.append(dataclasses.replace(swm.instruments[0], octave_shift=12))
    rows = [Row(note=49, instrument=1), Row(instrument=2), Row(note=49), Row(), Row()]
    swm.patterns[0] = Pattern(rows=rows, length=5)
    freq_hi = [row[1] for row in SWMPlayer(swm).render_grid(16)]
    assert freq_hi[3:9] == [NOTE_FREQ_HI[49]] * 6
    assert freq_hi[9:] == [NOTE_FREQ_HI[61]] * 7


@pytest.mark.parametrize("light,note", [(False, 49 - 5), (True, 49)])
def test_orderlist_transpose_reaches_dpitch_only_with_transposesupp(light, note):
    """CLEGATO's ``clc ; adc TRANSP,x`` is assembled out of the Light build (altplayers.inc:434)."""
    swm = _toy_swm()
    swm.sequences = [[Transpose(-5), PlayPattern(1), End()]] * 3
    if light:
        _light(swm)
    p = _play(swm, frames=SETUP_FRAMES + HR_FRAMES + 1)
    assert p.voices[0].dpitch == note
    assert p.regs[1] == NOTE_FREQ_HI[note]


def test_a_spare_slot_plays_the_images_instrument():
    """SEL_INS takes any slot below $3F (player.asm:1777); past the count it is the image's bytes."""
    swm = _toy_swm()
    spare = Instrument(control=0x10, attack=3)
    spare.ram = bytes([0x55]) * 0x100
    with_image(swm, spare_instruments={3: spare})
    p = SWMPlayer(swm)
    assert p._resolve_instrument(3) is spare  # pylint: disable=protected-access
    assert p._resolve_instrument(2) is p._empty  # pylint: disable=protected-access
    assert p._image(spare) == spare.ram  # pylint: disable=protected-access


@pytest.mark.parametrize("retain,sr", [(True, 0xAF), (False, 0xFF)])
def test_retainzeropage_restores_the_inits_playerzp_every_frame(retain, sr):
    """PLAYER pushes and pulls the caller's PLAYERZP (player.asm:609-613, 888-892)."""
    swm = _curins0_swm(Row(fx=0x6F))
    with_image(swm, zp_init=bytes([0, 0, 0, 0, 0xA3]))
    p = SWMPlayer(swm)
    assert p._zp == swm.player.zp_init  # pylint: disable=protected-access
    p.features = dataclasses.replace(p.features, retainzeropage=retain)
    assert p.render_grid(SETUP_FRAMES + 1)[SETUP_FRAMES][0x0D] == sr


@pytest.mark.parametrize(
    "fswitch,filtkbtrack,switch,track",
    [(True, True, 3, 0), (False, True, 7, 0x83), (True, False, 7, 0)],
)
def test_fladvan_reads_a_8x_third_column_as_fswitch(fswitch, filtkbtrack, switch, track):
    """FLADVAN (player.asm:1827-1836): from 1.4 a $80-$8F keyboard track is FSWITCH."""
    swm = _toy_swm()
    swm.instruments[0].filter_table = bytes([0x91, 0x40, 0x83, 0xFF])
    with_image(swm, kbtrack_fswitch=fswitch)
    p = SWMPlayer(swm)
    p.features = dataclasses.replace(p.features, filtkbtrack=filtkbtrack)
    for _ in range(SETUP_FRAMES + HR_FRAMES + 2):
        p.play_frame()
    assert (p.filter_switch, p.filter_kbd_track) == (switch, track)


def test_a_wf_jump_target_is_a_waveform_even_below_10():
    """WFAJUMP reads the target row straight into SEWFARP (player.asm:1965-1968); a $7E chord return reads on."""
    wf = bytes([0x41, 0x00, 0x00, 0xFE, 0x16, 0x00, 0x05, 0x7F, 0x00, 0x03, 0x00, 0x00, 0xFF])
    p = _play(_chord_swm(49, bytes([0x7E]), wf), frames=SETUP_FRAMES + 1)
    v = p.voices[0]
    v.wf_pos, v.arp_speed_counter, v.ptn_gate = 0x13, 0, 0xFF
    p._tick_wf_table(v)  # pylint: disable=protected-access
    assert (v.sid_ctrl, v.arp_speed_counter) == (0x05, 0x03)


def _kbtrack_player() -> SWMPlayer:
    p = _play(_toy_swm(), frames=SETUP_FRAMES + HR_FRAMES + 2)
    p.features = dataclasses.replace(p.features, pwkeybtrack=True, filtkbtrack=True)
    return p


def test_keyboard_tracking_indexes_by_the_whole_dpitch_byte():
    """``ldy DPITCH,x`` is unmasked in CKBDTRK / PKBDTRK (player.asm:1080, 2380)."""
    p = _kbtrack_player()
    v = p.voices[0]
    v.pw_kbd_track = p.filter_kbd_track = 0x10
    p.filter_controller_voice = v
    cutoffs, pulses = [], []
    for dpitch in (0xF5, 0x75):
        v.dpitch = dpitch
        p._write_regs()  # pylint: disable=protected-access
        cutoffs.append(p.snapshot()[0x16])
        pulses.append(p._wrpuls(v))  # pylint: disable=protected-access
    assert cutoffs[0] != cutoffs[1]
    assert pulses[0] != pulses[1]


@pytest.mark.parametrize("allghostregs", [False, True])
def test_the_pw_ghost_only_moves_on_a_setpwid_tick(allghostregs):
    """ENDPWTB alone writes SIDG.PLSW (player.asm:2376-2390); ALLGHOSTREGS re-emits the ghost."""
    p = _kbtrack_player()
    p.features = dataclasses.replace(p.features, allghostregs=allghostregs)
    v = p.voices[0]
    ghost = (v.sid_pw_hi, v.sid_pw_lo)
    p._wr(0xD403, 0x0E)  # pylint: disable=protected-access
    v.pw_kbd_track, v.dpitch, v.pw_written = 0x30, 0x40, False
    p._compose_sid_writes(v)  # pylint: disable=protected-access
    p._write_regs()  # pylint: disable=protected-access
    assert (v.sid_pw_hi, v.sid_pw_lo) == ghost
    assert p.snapshot()[3] == (ghost[0] if allghostregs else 0x0E)
    v.pw_written = True
    p._compose_sid_writes(v)  # pylint: disable=protected-access
    assert v.sid_pw_hi != ghost[0]


@pytest.mark.parametrize(
    "stores,wfctrl_smallfx,ctrl", [(True, True, 0x4A), (False, True, 0x41), (True, False, 0x41)]
)
def test_small_fx_e_merges_the_waveform_low_nibble_before_1_5(stores, wfctrl_smallfx, ctrl):
    """SMALFXE's ``jmp WRITEWF`` (player.asm:2748-2753) stores the merged nibble until 1.5."""
    swm = _fx_swm(0x02, 0x00)
    swm.patterns[0].rows[1] = Row(fx=0xEA)
    with_image(swm, wfctrl_fx_stores=stores)
    p = SWMPlayer(swm)
    p.features = dataclasses.replace(p.features, wfctrl_smallfx=wfctrl_smallfx)
    for _ in range(SETUP_FRAMES + 4):
        p.play_frame()
    assert p.regs[4] == ctrl


def test_a_spare_slot_with_a_zero_row_walks_it_as_the_wf_table():
    """A spare slot's table is the image's RAM: zeros at $10 are a real row (waveform 0, arp +0),
    so the pitch is looked up from FREQTBL every frame, not left to the "no WF table" hold."""
    swm = _toy_swm()
    spare = Instrument()
    spare.ram = bytes(0x100)
    swm.patterns[0] = Pattern(rows=[Row(note=49, instrument=3)] + [Row()] * 7, length=8)
    with_image(swm, spare_instruments={3: spare})
    grid = SWMPlayer(swm).render_grid(SETUP_FRAMES + HR_FRAMES + 6)
    assert {tuple(row[:2]) for row in grid[SETUP_FRAMES + HR_FRAMES + 1 :]} == {
        (NOTE_FREQ_LO[49], NOTE_FREQ_HI[49])
    }


def test_a_release_small_fx_leaves_the_attack_decay_a_write_ad_fx_set():
    """SMALFX6 merges into SR only, so BIGFX05's AD value is not put back to the instrument's."""
    p = SWMPlayer(_toy_swm())
    for _ in range(SETUP_FRAMES + HR_FRAMES + 1):
        p.play_frame()
    v = p.voices[0]
    p._apply_fx_columns(v, None, 0x05, 0x27, False)  # pylint: disable=protected-access
    p._apply_fx_columns(v, None, 0x6C, None, False)  # pylint: disable=protected-access
    assert v.sid_ad == 0x27 and v.sid_sr & 0x0F == 0x0C


def test_an_instrument_column_value_at_or_above_the_sel_ins_limit_keeps_the_current_instrument():
    """SEL_INS stores CURINS only below ``cmp #maxinstamount`` (a tune's own bound)."""
    swm = _toy_swm()
    swm.instruments.append(dataclasses.replace(swm.instruments[0], attack=5))
    swm.patterns[0] = Pattern(
        rows=[Row(note=49, instrument=1), Row(note=53, instrument=2)] + [Row()] * 4, length=6
    )
    with_image(swm, sel_ins_limit=2)
    p = _play(swm, frames=SETUP_FRAMES + 3 * 3 + 2)
    assert p.voices[0].instrument is swm.instruments[0]
    assert p.voices[0].dpitch == 53
