"""Smoke tests for :mod:`pysidwizard.player`."""

from __future__ import annotations

import warnings

import pytest

from pysidwizard import (
    End,
    Instrument,
    MainVolume,
    Pattern,
    PlayPattern,
    Row,
    SWMFile,
    SWMUnsupportedEffectWarning,
    Waveform,
    straight_tempo,
)
from pysidwizard.player import (
    NOTE_FREQ_HI,
    NOTE_FREQ_LO,
    SWMPlayer,
)


def _toy_swm() -> SWMFile:
    """One pattern, three sequences, one instrument — enough to exercise
    every per-frame code path."""
    inst = Instrument(
        name=b"TEST    ",
        attack=0,
        decay=0,
        sustain=0xF,
        release=0,
        first_waveform=Waveform.PULSE | 0x01,  # pulse + gate
        wf_table=bytes([0x41, 0x80, 0xFF]),  # one WF row: pulse+gate, NOP arp, no detune
        pw_table=b"\x88\x00\x00",  # set PW to 0x800 then end-of-table
        filter_table=b"",
    )
    pattern = Pattern(
        rows=[
            Row(note=49, instrument=1),  # middle C with instrument
            Row(),  # NOP
            Row(note=0x7E),  # gate-off
            Row(note=53, instrument=1),  # different pitch
        ],
        length=4,
    )
    return SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[pattern],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(2), straight_tempo(2))],
    )


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
    # Later frames report only changed registers, still as $00..$18 offsets.
    later = p.play_frame()
    assert later and all(0 <= r <= 0x18 for r, _ in later)
    assert len(later) <= 25
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
        subtune_tempos=[(straight_tempo(1), straight_tempo(1))],
    )
    p = SWMPlayer(swm)
    # HR phase runs for HR_FRAMES (2) frames before the WF table ticks
    # for the first time; advance through HR so the arp byte under test
    # actually gets evaluated.
    from pysidwizard.player import HR_FRAMES

    for _ in range(HR_FRAMES + 1):
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
        subtune_tempos=[(straight_tempo(2), straight_tempo(2))],
    )
    from pysidwizard.player import HR_FRAMES

    p = SWMPlayer(swm)
    # The filter table only walks during post-HR frames; HR_FRAMES of
    # test-bit playback come first.
    for _ in range(HR_FRAMES):
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
        subtune_tempos=[(straight_tempo(1), straight_tempo(1))],
    )
    from pysidwizard.player import HR_FRAMES

    p = SWMPlayer(swm)
    # First note triggers HR for HR_FRAMES frames, then chord cycling
    # begins: post-HR frame -> pitch 3, next -> pitch 7, next -> pitch 12,
    # next -> $7F loops back to pitch 3, and so on.
    pitches = []
    for _ in range(HR_FRAMES + 8):
        p.play_frame()
        pitches.append(p.voices[0].wf_arp_pitch)
    post_hr = pitches[HR_FRAMES:]
    assert post_hr == [3, 7, 12, 3, 7, 12, 3, 7], f"got {post_hr}"


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
    reference captures for flashitback show CTRL=$09 on song frame 0
    for v0 and v2 — see [[flashitback-dump-findings]]). We sample the
    very first frame of playback to verify."""
    from pysidwizard.player import HR_FRAMES

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
        subtune_tempos=[(straight_tempo(2), straight_tempo(2))],
    )
    p = SWMPlayer(swm)
    samples = []
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
    default) and ptn_gate=$FF post-_start_note, the HR CTRL is $09."""
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
        subtune_tempos=[(straight_tempo(1), straight_tempo(1))],
    )
    p = SWMPlayer(swm)
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
    from pysidwizard.player import HR_FRAMES, WF_ROW_STRIDE

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
    # Tempo=4 -> row 0 occupies frames 0-3, row 1 starts on frame 4.
    # Run through row 0 first (warm-up, no HR, WF table advances).
    for _ in range(4):
        p.play_frame()
    # Frame 4: row 1's note triggers _start_note, which resets wf_pos
    # to 0 AND starts the HR window. During HR wf_pos must stay at 0
    # (the WF table is paused).
    for f in range(HR_FRAMES):
        p.play_frame()
        assert (
            p.voices[0].wf_pos == 0
        ), f"HR frame {f}: WF table ticked (wf_pos={p.voices[0].wf_pos})"
    # On the first post-HR frame the WF table finally ticks.
    p.play_frame()
    assert p.voices[0].wf_pos == WF_ROW_STRIDE


def test_untriggered_voice_emits_no_register_writes():
    """A voice whose first pattern row carries no note and no instrument
    must produce zero writes to its $D40x register block — SID-Wizard's
    player gates the per-voice write loop on having seen a real note.

    Voice 0 plays a note; voices 1 and 2 sit on empty rows. The first
    frame's writes should include voice 0's seven registers and the four
    global registers, and nothing for voices 1 and 2.
    """
    inst = Instrument(
        name=b"V0      ",
        sustain=0xF,
        first_waveform=0x41,
        wf_table=bytes([0x41, 0x80, 0x00, 0xFF]),
    )
    pattern_active = Pattern(rows=[Row(note=49, instrument=1)], length=1)
    pattern_silent = Pattern(rows=[Row()], length=1)  # note=None instrument=None
    swm = SWMFile(
        sequences=[
            [PlayPattern(1), End()],  # voice 0: note + instrument
            [PlayPattern(2), End()],  # voice 1: empty row
            [PlayPattern(2), End()],  # voice 2: empty row
        ],
        patterns=[pattern_active, pattern_silent],
        instruments=[inst],
        subtune_tempos=[(straight_tempo(2), straight_tempo(2))],
    )
    p = SWMPlayer(swm)
    # Step past the HR test-bit frame into a steady CNTPLY2 frame. A
    # voice that never triggered leaves its register block at the reset
    # baseline (the memory model forward-fills untouched registers).
    p.play_frame()
    p.play_frame()
    regs = p.regs
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
    for _ in range(6):
        p.play_frame()
        frames.append((p.voices[0].sid_ctrl, p.voices[0].ptn_gate))
    # Tempo 3, note on row 2: frame 4 is TICK_0, frame 5 is TICK_1.
    for frame, gate_off in ((4, gate_off_tick0), (5, gate_off_tick1)):
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
    """``_toy_swm`` at tempo 3 with the given effect column on the first (note) row.

    Tempo 3 is the shortest period whose TICK_2 the player reaches on every row
    (see ``_tick_voice``), so rows past the first actually apply.
    """
    swm = _toy_swm()
    swm.subtune_tempos = [(straight_tempo(3), straight_tempo(3))]
    swm.patterns[0].rows[0] = Row(note=49, instrument=1, fx=fx, fx_value=fx_value)
    return swm


def _play(swm: SWMFile, frames: int = 12) -> SWMPlayer:
    p = SWMPlayer(swm)
    for _ in range(frames):
        p.play_frame()
    return p


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
    assert _play(_fx_swm(0x02, 0x00), frames=1).regs[4] == 0x00, "STRTSND baseline"
    assert _play(_fx_swm(0x04, 0x21), frames=1).regs[4] == 0x21


def test_big_fx_05_writes_the_attack_decay_register():
    """BIGFX05 = WRITEAD (player.asm:3512) writes $D405 directly (ALLGHOSTREGS_ON=0)."""
    assert _play(_fx_swm(0x02, 0x00), frames=1).regs[5] == 0x00, "STRTSND baseline"
    assert _play(_fx_swm(0x05, 0xA3), frames=1).regs[5] == 0xA3


def test_big_fx_06_writes_the_sustain_release_register():
    """BIGFX06 = WRITESR (player.asm:3514) writes $D406 directly (ALLGHOSTREGS_ON=0)."""
    assert _play(_fx_swm(0x02, 0x00), frames=1).regs[6] == 0xF0, "STRTSND baseline"
    assert _play(_fx_swm(0x06, 0x5C), frames=1).regs[6] == 0x5C


def test_big_fx_register_writes_apply_on_a_note_less_row():
    """PATT_FX also runs on rows with no note column (player.asm:1778)."""
    swm = _fx_swm(0x04, 0x21)
    swm.patterns[0].rows[1] = Row(fx=0x06, fx_value=0x39)
    p = _play(swm, frames=4)
    assert p.regs[6] == 0x39
    assert p.regs[4] == 0x41, "the WF table walk owns the waveform again by this frame"


def test_small_fx_4_sets_the_waveform_high_nibble():
    """SMALFX4 (player.asm:3298) merges the value nibble over WFGHOST's high nibble.

    ``lda WFGHOST,x ; jsr SETNIBH`` (player.asm:3216) keeps the low nibble --
    gate/sync/ring/test bits survive the waveform change.
    """
    swm = _fx_swm(0x02, 0x00)
    swm.patterns[0].rows[1] = Row(fx=0x48)
    p = _play(swm, frames=4)
    assert _play(_fx_swm(0x02, 0x00), frames=4).regs[4] == 0x41, "WF table baseline"
    assert p.regs[4] == 0x81, "waveform nibble replaced, gate bit kept"


def test_small_fx_e_is_a_no_op_in_1_94():
    """SMALFXE (player.asm:3407) computes a merge but its ``jmp WRITEWF`` is
    commented out, so the register is untouched -- and it must not report as
    unmodelled."""
    swm = _fx_swm(0x02, 0x00)
    swm.patterns[0].rows[1] = Row(fx=0xE0)
    with warnings.catch_warnings():
        warnings.simplefilter("error", SWMUnsupportedEffectWarning)
        p = _play(swm, frames=4)
    assert p.regs[4] == 0x41, "WFGHOST unchanged: the store is commented out upstream"


def test_big_fx_0f_sets_the_cutoff_high_byte():
    """BIGFX0F (player.asm:3563) writes CTFHGHO, the $D416 ghost."""
    assert _play(_fx_swm(0x02, 0x00), frames=1).regs[0x16] == 0x00
    assert _play(_fx_swm(0x0F, 0xC4), frames=1).regs[0x16] == 0xC4


def test_big_fx_1f_sets_filter_switches_and_resonance():
    """BIGFX1F (player.asm:3758) stores the value's nibbles into FSWITCH and RESONIB.

    Both are plain ``sta``s, so it also clears the per-voice routing bits until
    the next STRTSND re-ORs them (player.asm:2001).
    """
    p = _play(_fx_swm(0x1F, 0x38), frames=1)
    assert p.regs[0x17] == 0x38
    assert p.filter_switch == 0x08 and p.filter_resonance == 0x30


def test_small_fx_b_sets_the_filter_band_nibble():
    """SMALFXB (player.asm:3377) writes FLTBAND, the $D418 high nibble."""
    p = _play(_fx_swm(0xB2), frames=1)
    assert p.regs[0x18] == 0x2F, "band $2 over the default $F main volume"


def test_small_fx_f_sets_the_resonance_nibble():
    """SMALFXF (player.asm:3418) writes RESONIB, the $D417 high nibble."""
    p = _play(_fx_swm(0xF9), frames=1)
    assert p.regs[0x17] == 0x90


def test_filter_routing_bit_follows_the_instrument_filter_table():
    """FSWITCH's routing bits are owned by STRTSND's SETFLTP (player.asm:1999-2008)."""
    swm = _toy_swm()
    swm.instruments[0].filter_table = b"\x80\x40\x00\xff"
    p = _play(swm, frames=1)
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
    p = _play(_tables_swm(0x09, 0x01), frames=1)
    assert p.voices[0].wf_pos == 3


def test_big_fx_0a_jumps_to_a_pulse_table_row_and_clears_the_sweep():
    """BIGFX0A (player.asm:3530): PWTPOS := value*3 + instrument byte $0A, PWEEPCNT := 0."""
    swm = _tables_swm(0x0A, 0x01)
    base = 0x10 + len(swm.instruments[0].wf_table) + 1
    p = _play(swm, frames=1)
    assert p.voices[0].pw_pos == base + 3
    assert p.voices[0].pw_sweep_count == 0


def test_big_fx_0c_and_small_fx_c_set_the_arpeggio_speed():
    """BIGFX0C = SMALFXC (player.asm:3391): ARPSPED := value, ARPSCNT := $FF."""
    p = _play(_tables_swm(0x0C, 0x05), frames=1)
    assert (p.voices[0].arp_speed_reload, p.voices[0].arp_speed_counter) == (0x05, 0xFF)
    p = _play(_tables_swm(0xC3, 0x00), frames=1)
    assert (p.voices[0].arp_speed_reload, p.voices[0].arp_speed_counter) == (0x03, 0xFF)


def test_big_fx_07_selects_a_chord_by_full_byte():
    """BIGFX07 = SMALFX7 (player.asm:3325): CURCHORD := value, CHORDPOS := CHDPTRLO[value]."""
    p = _play(_tables_swm(0x07, 0x02), frames=1)
    assert p.voices[0].current_chord == 2
    assert p.voices[0].chord_pos == p._chord_starts[1]


def test_big_fx_0d_and_small_fx_d_detune_the_voice():
    """BIGFX0D = SETDETU (player.asm:3551) stores DETUNER; SMALFXD scales by 8."""
    assert _play(_fx_swm(0x0D, 0x05), frames=1).voices[0].detune == 5
    assert _play(_fx_swm(0x0D, 0xFB), frames=1).voices[0].detune == -5
    assert _play(_fx_swm(0xD2), frames=1).voices[0].detune == 16


def test_big_fx_0e_sets_the_pulse_width_high_nibble():
    """BIGFX0E (player.asm:3558): PWHIGHO := value & $0F."""
    assert _play(_fx_swm(0x0E, 0x3A), frames=1).voices[0].pw_hi == 0x0A


def test_big_fx_16_forces_the_vibrato_type():
    """BIGFX16 = FORCVI2 (player.asm:3698): SLIDEVIB := value & $30."""
    assert _play(_fx_swm(0x16, 0x37), frames=1).voices[0].slide_vib == 0x30


def test_small_fx_9_sets_the_vibrato_frequency_only():
    """SMALFX9 (player.asm:3349): VIBFREQU := nibble * 2 (``asl``)."""
    assert _play(_fx_swm(0x93), frames=1).voices[0].vibrato_period == 6


def test_small_fx_8_overrides_the_vibrato_amplitude_nibble():
    """SMALFX8 = VIBAMFX (player.asm:3338) keeps the instrument's frequency nibble."""
    swm = _fx_swm(0x84)
    swm.instruments[0].vibrato = 0x25
    v = _play(swm, frames=1).voices[0]
    assert v.vibrato_period == 10, "frequency nibble $5 comes from the instrument"
    assert (v.vibrato_freqmod_lo, v.vibrato_freqmod_hi) != (0, 0), "amplitude $4 applied"


def test_big_fx_08_with_zero_amplitude_clears_freqmod():
    """SETFMOD's ``beq wrFmodL`` (player.asm:2924) zeroes FREQMOD for amplitude 0."""
    swm = _fx_swm(0x08, 0x07)
    swm.instruments[0].vibrato = 0x84
    v = _play(swm, frames=1).voices[0]
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
    assert SWMPlayer._lookup_freqmod(index) == expected


@pytest.mark.parametrize("fx", [0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C])
def test_big_fx_1c_shifts_the_cutoff_and_17_to_1b_alias_it(fx):
    """BIGFX1C (player.asm:3706) sets FLSHIFT, which $D416 adds to the cutoff ghost.

    BIGFX17..BIGFX1B (player.asm:3700-3704) are bare labels immediately ahead of
    BIGFX1C, so all six BIGFXTABLE entries resolve to the same routine.
    """
    swm = _fx_swm(0x0F, 0x40)
    swm.patterns[0].rows[1] = Row(fx=fx, fx_value=0x10)
    swm.patterns[0].rows[2] = Row(fx=fx, fx_value=0xF0)
    assert _play(swm, frames=1).regs[0x16] == 0x40
    assert _play(swm, frames=4).regs[0x16] == 0x50
    assert _play(swm, frames=7).regs[0x16] == 0x30, "$80..$FF shifts down"


def _tempo_of(p: SWMPlayer, idx: int) -> tuple:
    v = p.voices[idx]
    return (v.tempo_left, v.tempo_right)


def test_big_fx_10_and_11_set_the_main_tempo_on_every_track():
    """MAINTMP (player.asm:3591) writes TEMPOTBL+0 and rewinds every track's TMPPOS.

    BIGFX10 (player.asm:3586) ``ora #$80``s the value, pinning the funk cycle to
    the one slot; BIGFX11 (player.asm:3627) splits the byte into two slots.
    """
    p = _play(_fx_swm(0x10, 0x04), frames=1)
    assert [_tempo_of(p, i) for i in range(3)] == [(4, 4)] * 3
    p = _play(_fx_swm(0x11, 0x53), frames=1)
    assert [_tempo_of(p, i) for i in range(3)] == [(5, 3)] * 3


def test_big_fx_13_and_14_set_the_tempo_of_one_track_only():
    """BIGFX13 / BIGFX14 (player.asm:3658 / 3669) write the track's own tempo slot."""
    swm = _fx_swm(0x13, 0x04)
    swm.sequences = [[PlayPattern(1), End()], [PlayPattern(2), End()], [PlayPattern(2), End()]]
    swm.patterns.append(Pattern(rows=[Row(note=49, instrument=1)] + [Row()] * 3, length=4))
    p = _play(swm, frames=1)
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
        v = _play(swm, frames=4).voices[0]
    assert v.slide_vib == 0x83
    assert (v.vibrato_freqmod_lo, v.vibrato_freqmod_hi) == SWMPlayer._lookup_freqmod(
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
        assert _play(swm, frames=4).unsupported_effects == {}
    swm.driver_type = 3
    with pytest.warns(SWMUnsupportedEffectWarning):
        assert _play(swm, frames=4).unsupported_effects == {("big-fx", 0x1E): 3}
