"""Filter-controller swap + global CWEPCNT regression tests.

These cover the two coupled bugs that the player TODO at HEAD~1
called out (and that none of the four reference SWMs exercise, since
each uses a single filter controller throughout with set-mode-starting
filter tables):

  Bug B: SETFLTP only made a voice the filter controller when the
         instrument's filter table started with a SET-mode byte
         ($80..$FE). Real SETFLTP claims controllership for ANY ft[0]
         that is not $00 or $FF — sweep rows ($01..$7F) included.

  Bug A: ``filt_sweep_count`` was per-voice, but the asm's CWEPCNT is a
         single global byte. A controller swap mid-song left the new
         controller's FILTPRG running from a stale 0 instead of
         inheriting the previous controller's countdown.

The bugs are coupled: the cleanest trigger for Bug A is a controller
swap driven by a sweep-starting filter table, which Bug B suppressed.
A synthetic two-voice SWM with sweep-starting filter tables exercises
both at once.
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
    Waveform,
    straight_tempo,
)
from pysidwizard.player import SWMPlayer
from tests._variant import with_image

# INITER zeroes SPDCNT, so play calls 0 and 1 are row 0's TICK_0 and TICK_1 and
# its note first applies on call 2 (TICK_2).
SETUP_FRAMES = 2


def _sweep_filter_instrument(name: bytes, delta: int) -> Instrument:
    """Instrument whose filter table is a single sweep row.

    byte0 = $10 (16-cycle countdown) is in the $01..$7F band that the
    asm's SETFLTP treats as "claim controller, walk this table". The
    pre-fix gate (``0x80 <= ft[0] <= 0xFE``) rejected this; the post-fix
    gate (``ft[0] not in (0x00, 0xFF)``) accepts it.
    """
    return Instrument(
        name=name,
        attack=0,
        decay=0,
        sustain=0xF,
        release=0,
        first_waveform=Waveform.PULSE | 0x01,
        wf_table=bytes([0x41, 0x80, 0xFF]),
        pw_table=b"\x88\x00\x00",
        filter_table=bytes([0x10, delta & 0xFF, 0x00]),
    )


def _silent_instrument() -> Instrument:
    return Instrument(
        name=b"SILENT  ",
        attack=0,
        decay=0,
        sustain=0xF,
        release=0,
        first_waveform=Waveform.PULSE | 0x01,
        wf_table=bytes([0x41, 0x80, 0xFF]),
        pw_table=b"\x88\x00\x00",
        filter_table=b"",
    )


def _swap_swm() -> SWMFile:
    """Two voices, each with a sweep-starting filter instrument.

    With tempo=6 a new row applies every 6 frames. v0's row 0 (a real
    note + filter instrument) applies at frame SETUP_FRAMES; v1's row 1
    (its note + filter instrument) applies 6 frames later — the mid-song
    controller swap.
    """
    pat_v0 = Pattern(rows=[Row(note=49, instrument=1)] + [Row()] * 7, length=8)
    pat_v1 = Pattern(rows=[Row(), Row(note=49, instrument=2)] + [Row()] * 6, length=8)
    pat_v2 = Pattern(rows=[Row()] * 8, length=8)
    return SWMFile(
        sequences=[
            [PlayPattern(1), End()],
            [PlayPattern(2), End()],
            [PlayPattern(3), End()],
        ],
        patterns=[pat_v0, pat_v1, pat_v2],
        instruments=[
            _sweep_filter_instrument(b"FLTUP   ", 0x05),
            _sweep_filter_instrument(b"FLTDN   ", 0xFA),
            _silent_instrument(),
        ],
        subtune_tempos=[(straight_tempo(6), straight_tempo(6))],
    )


def test_sweep_starting_filter_table_claims_controller():
    """Bug B: a filter table that starts with a sweep row ($01..$7F)
    must make its voice the filter controller, matching the asm's
    SETFLTP gate (``ft[0] not in (0x00, 0xFF)``)."""
    p = SWMPlayer(_swap_swm())

    # Row 0's TICK_2: PLAYER iterates voices reverse (v2, v1, v0). v0's
    # STRTSND runs last so it wins controllership. v0's filter table starts
    # with $10 — a sweep row the pre-fix gate would have rejected.
    for _ in range(SETUP_FRAMES):
        p.play_frame()
    assert p.filter_controller_voice is None
    p.play_frame()
    assert p.filter_controller_voice is p.voices[0]


def test_controller_swaps_to_later_voice_mid_song():
    """Bug B: a second voice triggering a sweep-starting filter
    instrument later in the song takes over as controller."""
    p = SWMPlayer(_swap_swm())
    # Run through v1's row 1 TICK_2 (tempo=6).
    for _ in range(SETUP_FRAMES + 7):
        p.play_frame()
    assert p.filter_controller_voice is p.voices[1]


def test_global_cwepcnt_persists_across_controller_swap():
    """Bug A: the sweep-cycle counter is a single global (CWEPCNT), not
    per-voice. When control swaps from v0 to v1 mid-sweep, the new
    controller continues from v0's accumulated count rather than
    restarting at 0."""
    p = SWMPlayer(_swap_swm())

    # Row 0's TICK_2 + 5 more: v0 controls, its lone $10 sweep row hasn't
    # completed (16 cycles), so the global counter climbs once per frame.
    for _ in range(SETUP_FRAMES + 6):
        p.play_frame()
    pre_swap = p.filter_sweep_count
    assert pre_swap >= 5, f"expected counter to accumulate under v0; got {pre_swap}"

    # Row 1's TICK_2: v1 takes control, but its FILTPRG is HR-locked this frame
    # (hr_timer=1), so the global counter must sit unchanged — STRTSND
    # does not clear CWEPCNT.
    p.play_frame()
    assert p.filter_controller_voice is p.voices[1]
    assert p.filter_sweep_count == pre_swap, (
        "filter_sweep_count must not reset on the HR-locked swap frame; "
        f"was {pre_swap}, now {p.filter_sweep_count}"
    )

    # The next frame: v1's HR clears and FILTPRG runs under v1 ownership. The
    # global counter ticks once more from v0's value. Under the OLD
    # per-voice model, v1's own counter would have been 0 and read 1
    # here — a value clearly distinct from the carried-over global.
    p.play_frame()
    assert p.filter_sweep_count == pre_swap + 1, (
        "global sweep counter must continue from v0's accumulated value; "
        f"expected {pre_swap + 1}, got {p.filter_sweep_count} "
        "(old per-voice model would have shown 1)"
    )


def _filter_instrument(control: int, filter_table: bytes) -> Instrument:
    return Instrument(
        control=control,
        sustain=0xF,
        first_waveform=Waveform.PULSE | 0x01,
        wf_table=bytes([0x41, 0x80, 0xFF]),
        pw_table=b"\x88\x00\x00",
        filter_table=filter_table,
    )


def _two_row_swm(first: Instrument, second: Instrument, second_row: Row) -> SWMFile:
    pat_v0 = Pattern(rows=[Row(note=49, instrument=1), second_row] + [Row()] * 6, length=8)
    silent = Pattern(rows=[Row()] * 8, length=8)
    return SWMFile(
        sequences=[[PlayPattern(1), End()], [PlayPattern(2), End()], [PlayPattern(2), End()]],
        patterns=[pat_v0, silent],
        instruments=[first, second],
        subtune_tempos=[(straight_tempo(6), straight_tempo(6))],
    )


def _filt_start(ins: Instrument) -> int:
    return 0x10 + len(ins.wf_table) + 1 + len(ins.pw_table) + 1


def test_a_zero_first_filter_byte_leaves_fltposi_with_the_controller():
    """SETFLTP's ``beq SWONFL`` (player.asm:1929) routes the voice without touching FLTCTRL / FLTPOSI."""
    first = _filter_instrument(0x00, bytes([0x90, 0x10, 0x00, 0x90, 0x20, 0x00]))
    second = _filter_instrument(0x00, bytes([0x00, 0x00, 0x00]))
    p = SWMPlayer(_two_row_swm(first, second, Row(note=50, instrument=2)))
    for _ in range(SETUP_FRAMES + 7):
        p.play_frame()
    assert p.filter_controller_voice is p.voices[0]
    assert p.voices[0].instrument is p.swm.instruments[1]
    assert p.voices[0].voice_in_filter
    assert p.filt_pos > _filt_start(first), "FLTPOSI kept walking the first instrument's table"


def test_the_filter_reset_switch_skips_setfltp():
    """FILTRESETSW's ``bmi ENDFILRESET`` (player.asm:1894) leaves FLTCTRL and FSWITCH untouched."""
    ins = _filter_instrument(0x80, bytes([0x90, 0x10, 0x00]))
    p = SWMPlayer(_two_row_swm(ins, ins, Row(note=50)))
    for _ in range(SETUP_FRAMES + 1):
        p.play_frame()
    assert p.filter_controller_voice is p.voices[0]
    p.filter_controller_voice = None
    for _ in range(6):
        p.play_frame()
    assert p.voices[0].note == 50
    assert p.filter_controller_voice is None


def test_the_filter_walk_reads_past_the_instrument_through_its_ram():
    """FLTPOSI indexes PLAYERZP like any table cursor, so it reads the image's following bytes."""
    ins = _filter_instrument(0x00, bytes([0x00, 0x00, 0x00]))
    pos = 0x45
    ram = bytearray(ins.memory_image() + b"\xff")
    ram += bytes(pos - len(ram)) + bytes([0xC1, 0x22, 0x00])
    ins.ram = bytes(ram)
    p = SWMPlayer(_two_row_swm(ins, ins, Row()))
    for _ in range(SETUP_FRAMES + 1):
        p.play_frame()
    v = p.voices[0]
    p.filter_controller_voice, p.filt_pos = v, pos
    p._tick_filt_table(v)  # pylint: disable=protected-access
    assert (p.filter_cutoff_hi, p.filter_resonance) == (0x22, 0x10)
    assert p.filt_pos == pos + 3


def test_ram_that_no_longer_holds_the_instrument_is_ignored():
    ins = _filter_instrument(0x00, bytes([0x90, 0x10, 0x00]))
    ins.ram = bytes(0x100)
    p = SWMPlayer(_two_row_swm(ins, ins, Row()))
    assert p._image(ins) == ins.memory_image()  # pylint: disable=protected-access


@pytest.mark.parametrize("rewinds", [True, False])
def test_an_ff_first_filter_table_keeps_fltctrl_on_the_track(rewinds):
    """SETFLTP's ``beq STOPFLT`` (player.asm:1995, 2003-2005) never clears FLTCTRL.

    STOPFLT points FLTPOSI at the new instrument's $FF; a pre-1.7 player has no
    STOPFLT, so FLTPOSI keeps walking, now through the new instrument's bytes.
    """
    first = _filter_instrument(0x00, bytes([0x90, 0x10, 0x00, 0x90, 0x20, 0x00]))
    second = _filter_instrument(0x00, b"")
    swm = _two_row_swm(first, second, Row(note=50, instrument=2))
    with_image(swm, filter_stop_rewinds=rewinds)
    p = SWMPlayer(swm)
    for _ in range(SETUP_FRAMES + 7):
        p.play_frame()
    assert p.filter_controller_voice is p.voices[0]
    assert not p.voices[0].voice_in_filter
    assert (p.filt_pos == _filt_start(second)) is rewinds


def test_a_swapped_in_sweep_starts_from_the_global_cutoff():
    """FISWEEP adds to the one CTFH_GHO / CTFL_GHO pair (player.asm:2222-2257), whoever set it."""
    first = _filter_instrument(0x00, bytes([0x90, 0x30, 0x00]))
    second = _filter_instrument(0x00, bytes([0x10, 0x08, 0x00]))
    pat_v0 = Pattern(rows=[Row(note=49, instrument=1)] + [Row()] * 7, length=8)
    pat_v1 = Pattern(rows=[Row(), Row(note=49, instrument=2)] + [Row()] * 6, length=8)
    swm = SWMFile(
        sequences=[[PlayPattern(1), End()], [PlayPattern(2), End()], [PlayPattern(3), End()]],
        patterns=[pat_v0, pat_v1, Pattern(rows=[Row()] * 8, length=8)],
        instruments=[first, second],
        subtune_tempos=[(straight_tempo(6), straight_tempo(6))],
    )
    p = SWMPlayer(swm)
    for _ in range(SETUP_FRAMES + 7):
        p.play_frame()
    assert p.filter_controller_voice is p.voices[1]
    assert p.filter_cutoff_hi == 0x30
    p.play_frame()
    assert (p.filter_cutoff_hi, p.filter_cutoff_lo) == (0x31, 0)


def test_sweep_row_advances_only_when_the_counter_equals_its_cycle_count():
    """FILTPRG's ``cmp CWEPCNT; beq`` is an equality test and ``inc CWEPCNT`` wraps at 256:
    a counter already past the row's cycle count (a controller swap hands it over) keeps
    sweeping until it comes round to that count."""
    p = SWMPlayer(_swap_swm())
    for _ in range(SETUP_FRAMES + 1):
        p.play_frame()
    start = p.filt_pos
    p.filter_sweep_count = 0x20  # the row's cycle count is $10
    p.play_frame()
    assert (p.filt_pos, p.filter_sweep_count) == (start, 0x21)
    p.filter_sweep_count = 0xFF
    p.play_frame()
    assert (p.filt_pos, p.filter_sweep_count) == (start, 0x00)
