"""Constants and tables transcribed from SID-Wizard's player.asm."""

from __future__ import annotations

from typing import List, Tuple

from pysidtracker.registers import (
    SID_BASE,
)

from ..features import PlayerFeatures
from ..model import (
    Instrument,
)

# SID register file base ($D400); re-exported from pysidtracker.registers for
# local callers (tests, tools) that import it from this module.
SID_REG_BASE = SID_BASE

# FREQTBL / FREQTBH from native/sources/include/player.asm. 96 notes;
# note 49 (SWM C-5) maps to ~261.6 Hz on a 985,248 Hz PAL SID.
_NOTE_FREQ_LO_HEX = (
    "071627384b5e7389a1bad4f00d2c4e7196bde7134274a8e01b599ce22c7bce27"
    "84e851c036b338c459f69d4e09d0a2816d677088b2ed3a9c13a04402dacee011"
    "64da753826408904b49cc022c8b4eb714c801208683880459068d6e398002410"
)
_NOTE_FREQ_HI_HEX = (
    "010101010101010101010101020202020202020303030303"
    "040404040505050606060707080809090a0a0b0c0d0d0e0f"
    "10111213141517181a1b1d1f20222427292b2e3134373a3e"
    "4145494e52575c62686e757c838b939ca5afb9c4d0ddeaf8"
)
NOTE_FREQ_LO = bytes.fromhex(_NOTE_FREQ_LO_HEX)
NOTE_FREQ_HI = bytes.fromhex(_NOTE_FREQ_HI_HEX)
assert len(NOTE_FREQ_LO) == 96
assert len(NOTE_FREQ_HI) == 96


def _empty_instrument() -> Instrument:
    """Return the depacker's blank instrument (``dummyihdr``, empty tables)."""
    return Instrument(control=0x1A, hr_decay=0xF, sustain=0xF, default_chord=1, first_waveform=0x09)


# Keyboard-track slope after ENDFREQTBH (player.asm:2980-2982), assembled only when CALCVIBRATO / PWKEYBTRACK / FILTKBTRACK is on.
FREQTBH_SLOPE = bytes([0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFE, 0xFF, 0xFF])

# EXPTABH (player.asm:2958-2982): 10 + 1 zero bytes, then FREQTBH, then the slope. FREQMOD's fine half and the PW / filter keyboard-track table.
EXPTABH_FREQTBH_OFFSET = 11
EXPTABH = bytes(EXPTABH_FREQTBH_OFFSET) + NOTE_FREQ_HI + FREQTBH_SLOPE
# EXPTRESHOLD = ENDFREQTBH - EXPTABH: where LOOKUPA switches to the rough table.
EXP_THRESHOLD = EXPTABH_FREQTBH_OFFSET + len(NOTE_FREQ_HI)
# MAXSLID (player.asm:2936) clamps Y to EXPTRESHOLD + (ENDFREQTBH - FREQTBH).
EXP_MAX_INDEX = EXP_THRESHOLD + len(NOTE_FREQ_HI)
# CALCVIBRATO_ON=0 (player.asm:2883): EXPTBASE = FREQTBH-1, so the threshold is 97.
NONCALC_THRESHOLD = len(NOTE_FREQ_HI) + 1
NONCALC_MAX_INDEX = NONCALC_THRESHOLD + len(NOTE_FREQ_HI)
# Bytes the assembler placed after FREQTBL (player.asm:3006 -- SEQ_FX's ``cmp #$A0`` in the shipped 1.94 build). Indexed loads past the table's 96th entry read them; the exact bytes are image-specific, so ``.sid`` inputs carry their own via :attr:`~pysidwizard.variant.PlayerImage.freq_table_tail` and this is only the fallback for a bare ``.swm``.
FREQTBL_PAST_END = 0xC9

# Longest past-FREQTBL run any indexed load can reach: ``ldy DPITCH,x`` is unmasked, so Y spans 0..255.
FREQ_TABLE_SPAN = 256
FREQ_TABLE_TAIL_LEN = FREQ_TABLE_SPAN - len(NOTE_FREQ_LO)


def freq_tables(features: PlayerFeatures, tail: bytes = b"") -> Tuple[bytes, bytes]:
    """FREQTBL / FREQTBH as the 6502 sees them: 96 notes then the following image bytes.

    ``tail`` is the assembled image from FREQTBL's end onward. FREQTBH sits
    *before* FREQTBL (player.asm:2958-3005), so reading past it walks the
    build-gated :data:`FREQTBH_SLOPE`, then FREQTBL itself, then ``tail``.
    """
    tail = tail or bytes([FREQTBL_PAST_END]) * FREQ_TABLE_SPAN
    slope = (
        FREQTBH_SLOPE
        if (features.calcvibrato or features.pwkeybtrack or features.filtkbtrack)
        else b""
    )
    pad = bytes([FREQTBL_PAST_END]) * FREQ_TABLE_SPAN
    lo = (NOTE_FREQ_LO + tail + pad)[:FREQ_TABLE_SPAN]
    hi = (NOTE_FREQ_HI + slope + NOTE_FREQ_LO + tail + pad)[:FREQ_TABLE_SPAN]
    return lo, hi


# Note-column effect-value range constants (mirror constants.py).
NOTE_FX_MIN = 0x60
GATE_OFF_FX = 0x7E
GATE_ON_FX = 0x7D

# WF / PW / Filter table markers and per-row strides.
WF_TABLE_JUMP = 0xFE
WF_TABLE_END = 0xFF
WF_ROW_STRIDE = 3
WF_ARP_NOP = 0x80
WF_ARP_REL_DOWN_BASE = 0xE0
WF_ARP_CHORD = 0x7F
PW_TABLE_JUMP = 0xFE
PW_TABLE_END = 0xFF
PW_ROW_STRIDE = 3
FILT_TABLE_JUMP = 0xFE
FILT_TABLE_END = 0xFF
FILT_ROW_STRIDE = 3  # set/sweep byte, cutoff_hi (or sweep delta), kbtrack

# Number of frames the hard-restart phase holds the test bit on
# (CTRL=$09 for the default $1A instrument control byte) before the
# WF table starts walking. Flashitback ref shows the test-bit CTRL
# emitted for exactly 1 frame (CSV 21 has $09, CSV 22 has $81 = WF row
# 0) — so the visible HR window is 1 frame, with the 2-frames-before
# pre-write supplying the "HR setup" phase (see _emit_pre_hr).
HR_FRAMES = 1
# Per-track variable stride: X is 0 / 7 / 14 for tracks 1..3 (player.asm:953).
TRACK_STRIDE = 7

# How many frames before the next row trigger the player writes HR-AD / HR-SR
# (and clears the gate) to prime the SID for the upcoming note's hard restart.
# Exactly the default build's TICK_0 + TICK_1 window (player.asm:1383/1626): only
# the Extra player's FASTSPEEDBIND collapses it, and that build is out of scope.
PRE_HR_LEAD_FRAMES = 2

# settings.cfg:304 DEFAULTHRADSR: the HR ADSR every instrument gets when HARDRESTYPES_ON is 0.
DEFAULT_HR_ADSR = 0x0F00

# Chord-table separator bytes. ``$7E`` marks "end of chord, return to
# WFARP table for the next row" (one-shot chord pluck); ``$7F`` marks
# "end of chord, loop back to start of this chord" (continuous arp).
# Both also delimit one chord from the next when parsing the table.
CHORD_END_RETURN = 0x7E
CHORD_END_LOOP = 0x7F


def _parse_chord_starts(chord_table: bytes) -> List[int]:
    """Return byte offsets where each chord begins in SID-Wizard's
    in-memory CHORDS table.

    SWMconvert.c line 2039 sets ``ChordIndex[0]=0; ChordIndex[1]=1`` —
    the in-memory CHORDS table has a 1-byte dummy at index 0 with the
    on-disk chord-table data copied to indices 1..N. So
    ``CHDPTRLO[1] = 1`` (chord 1 starts at byte 1), not 0. The
    1-based instrument ``default_chord`` indexes into the returned
    list; callers use the byte offset to read into a chord_table
    that includes the dummy prefix.
    """
    starts = [1] if chord_table else []
    for i, b in enumerate(chord_table):
        if b in (CHORD_END_RETURN, CHORD_END_LOOP) and i + 1 < len(chord_table):
            # On-disk byte i is in-memory byte i+1; the next chord
            # starts at on-disk index i+1 = in-memory index i+2.
            starts.append(i + 2)
    return starts


# Per-voice runtime state.
