"""Filter-controller swap + global CWEPCNT regression tests.

These cover the two coupled bugs that the player.py TODO at HEAD~1
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
    note + filter instrument) is pre-warmed and applies at frame 0; v1's
    row 1 (its note + filter instrument) applies at frame 6 — the
    mid-song controller swap.
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

    # Frame 0: PLAYER iterates voices reverse (v2, v1, v0). v0's STRTSND
    # runs last so it wins controllership. v0's filter table starts with
    # $10 — a sweep row the pre-fix gate would have rejected.
    p.play_frame()
    assert p.filter_controller_voice is p.voices[0]


def test_controller_swaps_to_later_voice_mid_song():
    """Bug B: a second voice triggering a sweep-starting filter
    instrument later in the song takes over as controller."""
    p = SWMPlayer(_swap_swm())
    # Run through frame 6, where v1's row 1 applies (tempo=6).
    for _ in range(7):
        p.play_frame()
    assert p.filter_controller_voice is p.voices[1]


def test_global_cwepcnt_persists_across_controller_swap():
    """Bug A: the sweep-cycle counter is a single global (CWEPCNT), not
    per-voice. When control swaps from v0 to v1 mid-sweep, the new
    controller continues from v0's accumulated count rather than
    restarting at 0."""
    p = SWMPlayer(_swap_swm())

    # Frame 0 + 5 more: v0 controls, its lone $10 sweep row hasn't
    # completed (16 cycles), so the global counter climbs once per frame.
    for _ in range(6):
        p.play_frame()
    pre_swap = p.filter_sweep_count
    assert pre_swap >= 5, f"expected counter to accumulate under v0; got {pre_swap}"

    # Frame 6: v1 takes control, but its FILTPRG is HR-locked this frame
    # (hr_timer=1), so the global counter must sit unchanged — STRTSND
    # does not clear CWEPCNT.
    p.play_frame()
    assert p.filter_controller_voice is p.voices[1]
    assert p.filter_sweep_count == pre_swap, (
        "filter_sweep_count must not reset on the HR-locked swap frame; "
        f"was {pre_swap}, now {p.filter_sweep_count}"
    )

    # Frame 7: v1's HR clears and FILTPRG runs under v1 ownership. The
    # global counter ticks once more from v0's value. Under the OLD
    # per-voice model, v1's own counter would have been 0 and read 1
    # here — a value clearly distinct from the carried-over global.
    p.play_frame()
    assert p.filter_sweep_count == pre_swap + 1, (
        "global sweep counter must continue from v0's accumulated value; "
        f"expected {pre_swap + 1}, got {p.filter_sweep_count} "
        "(old per-voice model would have shown 1)"
    )
