"""Per-frame SID-Wizard SWM player.

Consumes a :class:`pysidwizard.SWMFile` and emits the SID-register
writes that SID-Wizard's own 6502 player would produce on a real C64,
in per-frame order. The per-frame SID register output is validated
byte-for-byte against the ``anarkiwi/sidtrace`` sidplayfp oracle over
curated HVSC SID-Wizard exports (see ``tests/test_oracle.py`` — run
with ``pytest -m oracle``).

Most of the 1-SID feature surface is modelled: pattern playback with
note + instrument + small-FX + big-FX columns; note-column effects
(gate-off, sync, ring, portamento); per-instrument waveform / pulse-
width / filter tables (set, sweep, jump, end, arp-speed); chord
tables; vibrato; portamento; hard-restart; multispeed
(``frame_speed >= 2``); funktempo; tempo programs; the WRPITCH
detune-with-carry chain.

The player is parameterised by the compile-time feature flags of the build the
module was exported for: :attr:`SWMPlayer.features`, derived from the SWM
header's driver byte via :func:`pysidwizard.features.features_for_driver`.
Driver types 0 (normal), 1 (Medium) and 3 (Extra) are modelled; the Extra
build's DELAYSUPPORT_ON ``$1D`` / ``$1E`` are still reported as unmodelled
rather than silently dropped. Remaining builds land one at a time.

Multi-SID / SFX / slowdown / non-440 Hz tuning tables are out of
scope by intent. Any other effect this player does not implement is
reported (never swallowed) via
:class:`~pysidwizard.errors.SWMUnsupportedEffectWarning` and tallied in
:attr:`SWMPlayer.unsupported_effects`.

A tune plays via the generic ``pysidtracker`` CLI (``pysidtracker
info|reglog|wav <tune.sid>``), which pysidwizard registers for ``.sid``
files through the ``pysidtracker.formats`` entry point.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any, List, Optional

from pysidtracker.player import MemPlayer
from pysidtracker.registers import (
    PAL_CYCLES_PER_FRAME,
    PW_HI_REGS,
    SID_BASE,
    SID_VOICE_OFFSET,
)

from .constants import (
    INST_WF_TABLE_POS,
    PORTAMENTO_FX,
    RING_OFF_FX,
    RING_ON_FX,
    SYNC_OFF_FX,
    SYNC_ON_FX,
)
from .errors import SWMUnsupportedEffectWarning
from .features import PlayerFeatures, features_for_driver
from .model import (
    End,
    Instrument,
    Loop,
    MainVolume,
    Pattern,
    PlayPattern,
    SubtuneJump,
    SWMFile,
    TempoOverride,
    Transpose,
)

# --------------------------------------------------------------------------
# Constants extracted from SID-Wizard's player.asm.
# --------------------------------------------------------------------------

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

# EXPTABH (player.asm:2958-2982): 10 + 1 zero bytes, then FREQTBH, then an 8-byte
# slope extension. It is FREQMOD's fine half and the PW / filter keyboard-track table.
EXPTABH_FREQTBH_OFFSET = 11
EXPTABH = (
    bytes(EXPTABH_FREQTBH_OFFSET)
    + NOTE_FREQ_HI
    + bytes([0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFE, 0xFF, 0xFF])
)
# EXPTRESHOLD = ENDFREQTBH - EXPTABH: where LOOKUPA switches to the rough table.
EXP_THRESHOLD = EXPTABH_FREQTBH_OFFSET + len(NOTE_FREQ_HI)
# MAXSLID (player.asm:2936) clamps Y to EXPTRESHOLD + (ENDFREQTBH - FREQTBH).
EXP_MAX_INDEX = EXP_THRESHOLD + len(NOTE_FREQ_HI)
# calcVib at the clamp reads FREQTBL[96] -- one byte past the table, i.e. whatever the
# assembled image put there. In the shipped 1.94 player that is SEQ_FX's ``cmp #$A0``
# opcode (player.asm:3006); no tune can depend on it beyond this single clamped index.
FREQTBL_PAST_END = 0xC9

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


# --------------------------------------------------------------------------
# Per-voice runtime state.
# --------------------------------------------------------------------------


@dataclass
class VoiceState:
    """Per-voice (SID channel) playback state.

    Mirrors the fields SID-Wizard's player keeps per channel: a row of
    SID register operand bytes (the values latched into ``$D40x`` each
    frame), a small set of pattern / sequence pointers, and the
    per-instrument WF / PW / filter table positions.
    """

    # Sequence (orderlist) state.
    sequence: List = field(default_factory=list)
    seq_pos: int = 0
    # 0..2 SID channel index. A SubtuneJump repoints only THIS channel's
    # orderlist (player.asm SETSEQA/SETSEQB run for the channel in X
    # alone), so the walk needs to know which of the target subtune's
    # three orderlists to pick up.
    channel: int = 0
    transpose: int = 0
    # Orderlist Transpose commands are read EARLY (during the sequence
    # advance, before the new pattern's note triggers). SID-Wizard stores
    # the value in TRANSP2 and only copies it into the active TRANSP when
    # the new note is set up (player.asm:247,1237 — "DELAYER TO COMPENSATE
    # EARLY SEQ.FX"). We mirror that: _advance_sequence stashes here, and
    # _start_note promotes it to ``transpose`` so the change lands on the
    # new note rather than leaking into the previous note's held tail.
    transpose_pending: int = 0
    # Set when the voice reaches its orderlist terminator ($FE End or a
    # $FF loop that resolves to no pattern). Per player.asm $FE handling
    # (line ~1253: ``dec SPDCNT,x`` then ``jmp CNTPLAY``), an ended voice
    # STOPS advancing rows/patterns but KEEPS running its per-frame
    # instrument tables on the held note — it does NOT freeze. The
    # player-level ``SWMPlayer.finished`` flips once every voice is here.
    sequence_ended: bool = False

    # Pattern state.
    pattern: Optional[Pattern] = None
    pattern_row: int = 0  # next Row index to read
    # Row read at TICK_0 (speed_counter==0) that is queued for actual
    # application at TICK_2 (speed_counter==2). Mirrors SID-Wizard's
    # split between row read (TICK_0) and STRTSND (TICK_2). None when
    # there is no row pending application.
    pending_row: Any = None

    # Tempo state.
    tempo_left: int = 6
    tempo_right: int = 6
    tempo_toggle: int = 0  # alternates between left/right for funktempo
    speed_counter: int = 0  # frames spent on this row

    # Note / instrument state.
    note: int = 0  # discrete pitch (0..0x5F)
    instrument_idx: int = 0
    instrument: Optional[Instrument] = None
    octave_shift: int = 0  # signed semitones from instrument.octave_shift
    detune: int = 0  # signed adjustment from WF-table column 2

    # Hard-restart countdown: number of frames remaining in the HR phase.
    # While > 0, the voice emits HR-ADSR + CTRL=$08 (test bit) and does
    # NOT advance its WF / PW / Filter tables. Set to :data:`HR_FRAMES`
    # by :meth:`SWMPlayer._start_note` and decremented at end-of-tick.
    hr_timer: int = 0

    # Table positions: ABSOLUTE byte offsets from the instrument base
    # (SID-Wizard's WFTPOS / PWTPOS / FLTPOS), so 0 is the control byte of
    # the 16-byte header, not the first WF row. See Instrument.memory_image.
    wf_pos: int = 0
    wf_arp_pitch: int = 0  # signed offset added to the row's note
    wf_arp_absolute: bool = False  # True if WF arp set an absolute pitch
    wf_speed_counter: int = 0  # countdown of pending ARP-speed delay
    # SID-Wizard's ARPSCNT (= zero-page $2B per voice). Each WF tick
    # decrements by 1; if the post-decrement value is "positive" (bit
    # 7 clear) the player skips the WF row read this frame (SKIPWFT).
    # When it goes "negative" ($80..$FF), the player reloads from
    # ``instrument.arp_speed & $3F`` and proceeds with the row read.
    # Initial $FF matches STRTSND's ``sta ARPSCNT,x`` (player.asm
    # line 1422), which guarantees the very first post-note WF tick
    # reads row 0 immediately after the reload.
    arp_speed_counter: int = 0xFF
    # ARPSPED reload byte, LATCHED at STRTSND (player.asm line 1424-1427:
    # ``ldy #7 ; lda (PLAYERZP),y ; sta ARPSPED,x``). The WF tick reloads
    # ARPSCNT from this — and reads the multispeed flags (bits 6/7) from
    # it — NOT from the current instrument. A bare instrument-select
    # (no STRTSND) therefore keeps the previous instrument's arp pacing
    # until a real note retriggers.
    arp_speed_reload: int = 0

    # Vibrato state — mirrors VIDELCNT / VIBRACNT / VIBFREQU /
    # FREQMODL / FREQMODH at zero-page $52..$54 / $50..$51 per voice.
    vibrato_delay_counter: int = 0  # VIDELCNT, decrements until <0
    vibrato_freq_counter: int = 0  # VIBRACNT
    vibrato_period: int = 0  # VIBFREQU = freq_nibble * 2
    vibrato_freqmod_lo: int = 0  # FREQMODL
    vibrato_freqmod_hi: int = 0  # FREQMODH
    # SLIDEVIB-equivalent: control byte bits 4-5. 0 = INCVIBR, $10 =
    # NORMVIB, $20/$30 = other variants we don't yet model.
    slide_vib: int = 0
    # Accumulated signed 16-bit vibrato offset. SID-Wizard mutates
    # FREQLO/FREQHI ghosts in place each frame; we keep the offset
    # separate because ``_compose_sid_writes`` recomputes the base
    # freq from the row note + arp + transpose every frame and would
    # otherwise wipe the vibrato delta. ``_compose_sid_writes`` adds
    # this to the base freq; ``_tick_vibrato`` mutates it.
    vibrato_offset: int = 0

    # SID-Wizard's WRPITCH does ``ADC DETUNER,x`` which inherits the
    # 6502 carry flag set by the WF-table walk's branching. When the WF
    # walk takes a code path that leaves carry SET (past-end ``$FF``,
    # NOP arp ``$80``), WRPITCH adds an extra +1 to SID FREQ_LO. This
    # flag mirrors that effect — set each frame by the WF-table walk
    # and applied in ``_compose_sid_writes``.
    wf_pitch_carry: bool = False
    # True when the current frame's WF arp processed a RELPTCH /
    # ABSPTCH / REL_DOWN row (= one that writes FREQLO/FREQHI ZP).
    # Cleared at the top of each WF tick; set inside the WF row
    # decode. Used by ``_tick_voice`` to suppress the vibrato
    # ADD/SUB step's NET CONTRIBUTION this frame — SID-Wizard's
    # CNTPLY2 runs NORMVIB -> ADDFREQ which mutates FREQLO/FREQHI
    # ZP, then WFARPTB -> RELPTCH overwrites those bytes, so any
    # vibrato shift becomes invisible when the WF arp walks a real
    # pitch every frame.
    wf_did_pitch_write: bool = False
    # True when the current frame's WFARPTB took the SKIPWFT branch
    # (= ARPSCNT was positive after dec, bpl taken). _tick_voice
    # uses this to decide whether HARDRST's ``cpy #$3F`` carry-SET
    # (when CURIFX > $3F) propagates to WRPITCH's adc DETUNER.
    skipwft_this_tick: bool = False

    # True for the most recent frame where compose ran in HR phase
    # (hr_timer > 0 -> STRTSND test-bit tick). HR phase only writes
    # AD/SR/CTRL/FREQ_HI ghosts; emit uses this to skip the FREQ_LO,
    # PW_LO, PW_HI writes that SID-Wizard's STRTSND doesn't touch.
    in_hr_this_frame: bool = False
    # True for TICK_2 of a legato / portamento / NPORTAM row. SID-Wizard's
    # CLEGATO path at this phase ends in ``LEGATOO jsr INSPTFX ; jmp
    # WRWFGHO`` — only $D404 (CTRL) gets written. FREQ/PW/AD/SR retain
    # their previous-frame values via SID register persistence. Without
    # this flag we emit a fresh $D40A computed from the just-updated
    # DPITCH, which mispredicts on legato boundaries.
    in_tick2_legato_this_frame: bool = False
    # True if some code path in THIS frame's tick wrote sid_ad/sr (= a
    # STRTSND-equivalent, an HRGTOFF-equivalent, or a $2x/$3x/$5x/$6x
    # small-FX). SID-Wizard's CNTPLY1/2 steady-state path doesn't touch
    # SIDG.AD/SR, so we mirror that by only emitting AD/SR when this
    # flag is set. Reset at top of _tick_voice / _tick_voice_multispeed.
    adsr_emit_required: bool = False

    pw_pos: int = 0
    pw_hi: int = 0
    pw_lo: int = 0
    pw_sweep_count: int = 0  # cycle countdown when sweeping
    # PKBDTRK byte: column 2 of the PW table row that's currently
    # "in effect". SID-Wizard's PWADVAN reads this byte after each
    # SET / sweep-end and stashes it for the next WRPULS, where it
    # picks an EXPTAB-based per-note adjustment to PWHIGHO.
    pw_kbd_track: int = 0

    filt_pos: int = 0
    filt_hi: int = 0
    filt_lo: int = 0
    voice_in_filter: bool = False

    # ZP-level frequency ghosts (= SID-Wizard's FREQLO,x / FREQHI,x at
    # zero-page). These hold the PRE-carry, PRE-detune value — the
    # output of the WF arp + vibrato + portamento chain BEFORE
    # WRPITCH applies the detune-with-carry 8-bit ADC. SID-Wizard
    # reads these (not the SID register) when seeding portamento /
    # legato / NPORTAM slides. The ``ADC DETUNER`` carry adjustment
    # only lands on the SID write below, not back on the ghost.
    zp_freq_lo: int = 0
    zp_freq_hi: int = 0

    # SID operand slots emitted each frame. ``sid_freq_lo`` / ``_hi``
    # are the POST-carry, POST-detune values — what WRPITCH actually
    # writes to $D400 / $D401. Read these for diff / dedup; read
    # ``zp_freq_lo`` / ``_hi`` for upstream pitch-slide calculations.
    sid_freq_lo: int = 0
    sid_freq_hi: int = 0
    sid_pw_lo: int = 0
    sid_pw_hi: int = 0
    sid_ad: int = 0
    sid_sr: int = 0
    sid_ctrl: int = 0  # full SID voice-control byte (waveform + flags)

    # Gate-state shadow: 0xFF when gate is held on, 0xFE when off. Used
    # exactly as PTNGATE,x in player.asm to mask the WF-table waveform.
    ptn_gate: int = 0xFE

    # A new-note row's FX columns, deferred until after STRTSND's register writes
    # (player.asm:2054). ``(inst_smallfx, fx, fx_value, portamento_applied)`` or None.
    deferred_fx: Any = None

    # Small-fx ADSR overrides. When a pattern row's fx column is
    # ``$5x`` (set note-volume = sustain nibble) or ``$6x`` (set
    # note-release), the corresponding instrument ADSR field is
    # overridden until the next ``_start_note`` call. ``None`` =
    # use the instrument's default value.
    attack_override: Optional[int] = None
    decay_override: Optional[int] = None
    sustain_override: Optional[int] = None
    release_override: Optional[int] = None

    # ``triggered`` flips True the first time ANY pattern column touches
    # the voice (real note, gate-off / note-FX, instrument-only legato,
    # or main-volume fx). SID-Wizard's player skips the per-voice
    # register-write loop entirely until something happens on that voice.
    triggered: bool = False
    # ``note_played`` flips True on the first real note trigger (note
    # column < $60 with a row that calls _start_note). Currently
    # informational only.
    note_played: bool = False

    # Chord-table position. ``current_chord`` is the 1-based chord index
    # (0 = no chord active). ``chord_pos`` is a byte index into the SWM
    # chord table — the next pitch the chord handler will play when the
    # WF table's arp byte hits ``$7F``.
    current_chord: int = 0
    chord_pos: int = 0


# --------------------------------------------------------------------------
# Player.
# --------------------------------------------------------------------------


class SWMPlayer(MemPlayer):
    """Per-frame SID-Wizard player, as a :class:`~pysidtracker.player.MemPlayer`.

    Construct from a :class:`pysidwizard.SWMFile`. The inherited
    :meth:`~pysidtracker.player.MemPlayer.play_frame` returns the
    ``(reg_offset, value)`` writes that changed this frame, and
    :meth:`~pysidtracker.player.MemPlayer.render_grid` yields the
    forward-filled per-frame register grid the shared sidtrace oracle
    compares against.

    This is a pure-Python transcription of SID-Wizard's 6502 player: the
    inherited 64 KiB memory image is used only as the ``$D400`` SID register
    file that :meth:`_frame` writes into and :meth:`snapshot` reads back.
    """

    def __init__(self, swm: SWMFile, features: Optional[PlayerFeatures] = None) -> None:
        self.swm = swm
        # The build this module was exported for (SWM driver byte = PLAYERTYPE).
        self.features = features or features_for_driver(swm.driver_type)
        self._unsupported_effects: dict = {}
        super().__init__(b"", 0)

    def _init(self, subtune: int = 0) -> None:
        swm = self.swm

        # Frame rate. SID-Wizard's frame speed is the value at offset 4
        # of the header (``FSPEEDPOS`` / 1..8). Each frame the player
        # advances all tables once; ``frame_speed > 1`` re-runs the
        # tables that many times per "frame" but emits a single SID
        # update at the end. For our purposes we model 1× speed and
        # let the CIA timer in pyresidfp pace the audio. ``cycles_per_frame``
        # is exposed so the reglog/oracle framing can size each frame.
        self.cycles_per_frame = PAL_CYCLES_PER_FRAME // max(1, swm.frame_speed)

        # In-memory CHORDS table — the on-disk chord_table prefixed
        # with a 1-byte dummy at index 0, mirroring SID-Wizard's
        # SWMconvert.c line 2039 layout (``ChordIndex[1]=1``). Chord
        # playback at WF arp $7F reads from this table; the dummy
        # byte 0 is never indexed because CHDPTRLO[N] >= 1 for all
        # in-use chords.
        self._chord_table: bytes = b"\x00" + swm.chord_table if swm.chord_table else b""
        # Chord-table base offsets, one per (1-indexed) chord. Computed
        # once at construction so each ``$7F`` arp hit is an O(1) lookup.
        self._chord_starts: List[int] = _parse_chord_starts(swm.chord_table)
        # Instrument memory images, keyed by object identity (see _image).
        self._inst_images: dict[int, bytes] = {}

        # Per-voice state. SID-Wizard assigns the SWM sequences in the
        # order [V0, V1, V2] for subtune 0 (sequences 0, 1, 2).
        # Tempo decoding (mirrors player.asm's TEMPOTBL / TMPPOS logic
        # around the ``ora #$80`` "SINGLE TEMPO" trick at line ~2927):
        # each subtune tempo pair is two bytes; bit 7 of each byte signals
        # "this slot is a fixed tempo, do not advance to the partner slot".
        # When BOTH bytes have bit 7 set, the per-track TMPPOS stays at
        # slot 0 and the row period is just ``left & 0x7F`` — a straight
        # tempo. Only when at least one byte has bit 7 CLEAR does TMPPOS
        # toggle between the two slots, producing the funktempo swing
        # of alternating row periods.
        tempo_left, tempo_right = swm.subtune_tempos[0] if swm.subtune_tempos else (6, 6)
        straight = (tempo_left & 0x80) and (tempo_right & 0x80)
        if straight:
            tl = tr = tempo_left & 0x7F
        else:
            tl = tempo_left & 0x7F
            tr = tempo_right & 0x7F
        self.voices: List[VoiceState] = []
        for i in range(3):
            v = VoiceState(channel=i)
            if i < len(swm.sequences):
                v.sequence = swm.sequences[i]
            v.tempo_left = tl
            v.tempo_right = tr
            # Pre-warm: SID-Wizard's TICK_0 + TICK_1 of row 0 happen
            # during the F1 transition (frames detected by
            # ``detect_transition_end``); our compared ``frame 0`` aligns
            # with ref's TICK_2 of row 0. Pre-stash row 0 in pending_row
            # and set speed_counter=2 so the first ``play_frame`` runs
            # TICK_2 of row 0 directly, matching ref's frame-0 STRTSND
            # write.
            v.speed_counter = 2
            self.voices.append(v)
        # $D418 and its pending SEQVOLU nibble must exist before the
        # pre-stash _advance_row loop, which performs the SEQVOLU->MAINVOL
        # copy on the low nibble of filter_mode_vol.
        self.filter_mode_vol = 0x0F  # $D418
        self.seqvolu = 0x0F  # SEQVOLU_var: pending master-volume nibble
        # Pre-stash row 0 for each voice. Done after voice list is
        # populated so _advance_sequence can read the sequence.
        for v in self.voices:
            self._advance_row(v)

        # Global filter / volume state. SID-Wizard's volume defaults to
        # $F (max); the filter cutoff and mode bits are configured by
        # the most-recent SET row of any voice's filter table.
        # ``filter_resonance`` is the upper nibble of $D417 (set by
        # filter-table SET rows); ``filter_mode_vol`` keeps the upper
        # nibble (band switches LP/BP/HP from the same SET rows) and
        # the lower nibble (master volume, set by small-fx $Ax).
        # The lower nibble of $D417 (routing) is composed each frame
        # by :meth:`_emit_writes` from per-voice ``voice_in_filter``
        # flags ORed against the per-voice routing bit table.
        self.filter_cutoff_hi = 0x00
        self.filter_cutoff_lo = 0x00
        self.filter_resonance = 0x00  # high nibble of $D417
        # FSWITCH (player.asm:1085): the low nibble of $D417 -- per-voice routing bits
        # maintained by STRTSND's SETFLTP (player.asm:1999-2008), plus the filter-external
        # bit that only BIGFX1F writes. Initialised to 0 by the player's INITER (line 718).
        self.filter_switch = 0x00
        # FLSHIFT (player.asm:1110): a signed byte BIGFX1C adds to the cutoff-high
        # ghost on every $D416 write. INITER zero-initialises it (player.asm:694).
        self.filter_shift = 0x00
        # SID-Wizard's FilterProgram is shared across voices: only the
        # one voice currently marked as filter-controller (FLTCTRL+1
        # operand byte at $1xxx) actually walks its filter table.
        # Other voices' filter_table-bearing instruments only set
        # their routing bit (voice_in_filter); the cutoff sweep is
        # owned by the controller. ``None`` = no controller selected
        # yet (matches the post-INITER default; first STRTSND of a
        # SET-row-starting instrument assigns).
        self.filter_controller_voice: Optional[VoiceState] = None
        # CWEPCNT (player.asm ~line 1761) is a single global byte —
        # the sweep-cycle countdown for whichever voice currently owns
        # FilterProgram. Mirror that here as a player-level (not per-
        # voice) counter.
        self.filter_sweep_count = 0

        self.frame_idx = 0
        self.finished = False

        # Multispeed state. For ``frame_speed > 1``, SID-Wizard's IRQ
        # chain fires ``frame_speed`` times per video frame: one
        # single-speed call (= ``playsub`` -> PLAYER) plus
        # ``frame_speed - 1`` multispeed calls (= ``mulpsub`` ->
        # MULPLY). PLAYER fires first within each video frame, then
        # the multispeed calls. ``_multispeed_phase`` cycles 0 ..
        # ``frame_speed - 1`` across consecutive ``play_frame`` calls:
        # phase 0 -> PLAYER tick, phase >= 1 -> MULPLY tick.
        self._multispeed_phase = 0

    # ---- per-frame tick ----------------------------------------------

    def _frame(self) -> None:
        """Advance one player tick, writing this frame's SID registers to memory.

        For ``frame_speed > 1`` tunes (e.g. euphoria's frame_speed=2),
        consecutive calls alternate between full PLAYER ticks and
        MULPLY (multispeed) ticks. MULPLY ticks only walk the WF
        table (and PW / Filter when the instrument's arp_speed has
        the bit-6 / bit-7 multispeed flags set) without advancing
        rows or running HR setup.
        """
        is_multispeed_tick = self.swm.frame_speed > 1 and self._multispeed_phase != 0
        # SID-Wizard's PLAYER calls DOTRACK in REVERSE voice order
        # (channel 3, 2, 1 = v2, v1, v0; player.asm lines 613-656).
        # State updates that race across voices in the same frame
        # (notably FLTCTRL — the global filter-controller voice — set
        # by the last STRTSND of the frame) only match ref when we
        # mirror that order: v0's STRTSND runs LAST and so wins FLTCTRL.
        for v_idx in range(len(self.voices) - 1, -1, -1):
            voice = self.voices[v_idx]
            if is_multispeed_tick:
                self._tick_voice_multispeed(voice)
            else:
                self._tick_voice(voice)
        self.finished = all(v.sequence_ended for v in self.voices)
        self._write_regs()
        self.frame_idx += 1
        self._multispeed_phase = (self._multispeed_phase + 1) % max(1, self.swm.frame_speed)

    # ---- voice tick ---------------------------------------------------

    def _tick_voice(self, v: VoiceState) -> None:
        """One frame's worth of advance for ``v``: tempo / row / table."""
        # Reset the per-tick LEGATOO flag at the START of every tick so it
        # only sticks when set explicitly below in the legato branch and
        # only governs THIS tick's emit. Otherwise a PLAYER-tick legato
        # would leak the flag into a following MULPLY tick which DOES
        # emit all SID writes via SID-Wizard's MULCNTP -> WRPULS chain.
        v.in_tick2_legato_this_frame = False
        v.adsr_emit_required = False
        # A voice that never played a note and has no pattern is a true
        # no-op. A voice that REACHED its orderlist terminator
        # (``sequence_ended``) is NOT frozen — it keeps ticking its
        # instrument tables on the held note below (player.asm $FE ->
        # CNTPLAY); only its row/pattern advance is pinned.
        if v.pattern is None and not v.triggered:
            return
        # Decide whether this frame is a "row tick" (advance pattern row)
        # or a between-rows frame (just continue table execution).
        current_tempo = v.tempo_left if v.tempo_toggle == 0 else v.tempo_right
        if current_tempo == 0:
            current_tempo = 6
        # Capture pre-increment speed_counter so we can detect the
        # ``PRE_HR_LEAD_FRAMES`` frame (= tempo - PRE_HR_LEAD_FRAMES)
        # after the increment / wrap. Used by _emit_pre_hr below.
        pre_incr_sc = v.speed_counter
        if v.speed_counter == 0 and not v.sequence_ended:
            # TICK_0: read the row into v.pending_row. The actual
            # SEL_INS / note trigger / table reset happens at TICK_2
            # (pre_incr_sc==2) below, mirroring SID-Wizard's TICK_0 ->
            # TICK_1 -> TICK_2 split. The HR-AD/SR write happens AFTER
            # compose (see end of this function) so it isn't clobbered
            # by the previous instrument's main AD/SR.
            # Once the orderlist terminator is reached the row advance is
            # pinned (player.asm ``dec SPDCNT,x``); the held note's
            # tables keep running on subsequent frames.
            self._advance_row(v)
            v.tempo_toggle ^= 1
        v.speed_counter += 1
        if v.speed_counter >= current_tempo:
            v.speed_counter = 0
        # TICK_2: apply the row's note / instrument / fx now, two frames
        # after the row read. _apply_row may call _start_note (sets
        # hr_timer, resets tables, gate-on). The compose call below then
        # writes main AD/SR and CTRL=$09/first_waveform for the new
        # note's HR test-bit frame.
        applied_portamento_row = False
        apply_sc = self._apply_tick(current_tempo)
        if pre_incr_sc == apply_sc and v.pending_row is not None:
            row = v.pending_row
            # Detect portamento- or legato-row apply BEFORE clearing
            # pending_row. SID-Wizard's TICK_2 CLEGATO path takes the
            # ``beq LEGATOO ; jsr INSPTFX ; jmp WRWFGHO`` route in
            # three cases: CURFX2=$03 (portamento), CURIFX=$3F (legato),
            # or SLIDEVIB=$FF (note-FX portamento pre-signed). All
            # share the property that DPITCH/SLIDEVIB/FREQMOD update,
            # INSPTFX dispatches any small/big-FX, then only WFGHOST
            # is written — WF/PW/Filter/Vibrato walks AND PORTAME slide
            # are skipped on this frame.
            if (
                row.note is not None
                and 0 < row.note < NOTE_FX_MIN
                and (row.fx == 0x03 or row.instrument == 0x3F or v.slide_vib == 0xFF)
            ):
                applied_portamento_row = True
            self._apply_row(v, v.pending_row)
            v.pending_row = None
        if applied_portamento_row:
            # CLEGATO -> LEGATOO -> INSPTFX -> WRWFGHO: only the SID
            # waveform / control register gets a fresh write this frame.
            # The portamento seed in _begin_portamento (and the LEGATO
            # branch in _apply_row) already subtracted the previous
            # frame's wf_pitch_carry from previous_freq, so compose's
            # ``base[target] + offset + (carry ? 1 : 0)`` reproduces the
            # previous SID FREQ regardless of whether carry is set.
            # Leave wf_pitch_carry untouched here so the inherited carry
            # state is consistent with subsequent ticks.
            v.in_tick2_legato_this_frame = True
            self._compose_sid_writes(v)
            return
        v.in_tick2_legato_this_frame = False
        # Hard-restart phase: SID-Wizard's player holds the test bit on
        # (CTRL=$09 for the default $1A control byte), loads post-HR
        # ADSR, and DOES NOT advance the per-voice instrument tables
        # for ``HR_FRAMES`` (1) frame. The tables resume on the first
        # post-HR frame.
        if v.hr_timer > 0:
            self._compose_sid_writes(v)
            self._run_deferred_fx(v)
            v.hr_timer -= 1
            return
        # Staccato HR exit (player.asm spec §7 ``lda #$18; sta WFGHOST,x;
        # sta SIDG.WAVE,x; rts``): when HRGTOFF fires for an instrument
        # with control bit 2 set, the SID write goes out as $18 (test +
        # waveform bit 4) and the routine RTS's without walking tables.
        # Mirror that here -- skip WF/PW/Filter/Vibrato and let pre-HR
        # emit set sid_ctrl=$18. ARPSCNT must NOT decrement (the asm's
        # WFARPTB never runs) so we also skip the per-instrument arp
        # counter update.
        if self._is_staccato_hr_tick(v, pre_incr_sc, apply_sc):
            self._maybe_emit_pre_hr(v, v.pending_row, pre_incr_sc)
            return
        # New-note setup window: TICK_0 / TICK_1 of a row whose pending
        # data has a new note. In SID-Wizard these ticks take the HARDRST
        # -> HRGTOFF -> HRENDER -> WFARPTB path, skipping SETPWID and FILTPRG
        # (PW / Filter tables are NOT walked). v.pending_row is non-None
        # at pre_incr_sc 0 and 1 of a new-note row; it gets cleared at
        # TICK_2 (pre_incr_sc==2).
        in_new_note_setup = (
            v.pending_row is not None
            and self._pending_row_has_new_note(v)
            and not self.features.vibslidealways
        )
        # SID-Wizard's CNTPLY2 runs VIBSLIDE BEFORE WFARPTB
        # (player.asm line ~1632): vibrato first mutates FREQLO/FREQHI
        # ZP, then WFARPTB's RELPTCH/ABSPTCH/chord-pitch paths
        # OVERWRITE those ZP bytes. So when WF arp writes pitch, the
        # vibrato step from this frame has zero net effect. Match by
        # ticking vibrato first; _tick_wf_table wipes vibrato_offset
        # on pitch-write rows (RELPTCH / ABSPTCH / chord).
        if not in_new_note_setup:
            self._tick_vibrato(v)
        self._tick_wf_table(v)
        # HARDRST's ``cpy #$3F`` (player.asm line 1149) leaves carry SET
        # when CURIFX is in $40..$7F (small-FX in the inst column). The
        # carry then propagates through ``bmi wasinst`` -> ``and (CTRL)
        # ; beq HRENDER`` -> ``jmp WFARPTB`` -> ``dec ARPSCNT ; bpl
        # SKIPWFT`` -> ``jmp ENDWFTB`` (none affect carry) into ``adc
        # DETUNER`` = +1 on SID FREQ_LO. Only matters when SKIPWFT was
        # taken (row-read's CMP #$80 / CMP #$E0 overwrites carry).
        #
        # The cpy #$3F runs only when HARDRST enters NEWNOTE — i.e.:
        # CURNOT in 1..$5F (pitched note, not NOP, not note-FX),
        # CURFX2 != $03 (not big-FX portamento), SLIDEVIB != $FF (not
        # NPORTAM pre-flag). Any other path takes NONEWNO -> CNTPLAY
        # and never executes cpy #$3F.
        prow = v.pending_row
        if (
            v.skipwft_this_tick
            and not self.features.vibslidealways
            and pre_incr_sc < apply_sc
            and prow is not None
            and prow.note is not None
            and 1 <= prow.note < 0x60
            and prow.fx != 0x03
            and v.slide_vib != 0xFF
            and prow.instrument is not None
            and 0x40 <= prow.instrument <= 0x7F
        ):
            v.wf_pitch_carry = True
        if not in_new_note_setup:
            self._tick_pw_table(v)
            self._tick_filt_table(v)
        self._compose_sid_writes(v)
        # TICK_0 (pre_incr_sc==0) and TICK_1 (pre_incr_sc==1) of a
        # pending new-note row: SID-Wizard's HARDRST -> HRGTOFF writes
        # HR-AD/SR for the upcoming instrument and ANDs the gate bit
        # off WFGHOST. The compose call just above wrote the previous
        # note's main AD/SR; this re-assertion overrides with HR
        # values which then persist (via dedup) until STRTSND/SETADSR
        # rewrites them at TICK_2.
        if pre_incr_sc < apply_sc and v.pending_row is not None:
            self._maybe_emit_pre_hr(v, v.pending_row, pre_incr_sc)

    def _tick_voice_multispeed(self, v: VoiceState) -> None:
        """Multispeed (MULPLY / MULCNTP at player.asm line 948+) tick
        for ``v``: walks the WF table (and PW / Filter when the
        instrument's arp_speed has bit 6 / bit 7 set respectively)
        and re-computes SID writes. Does NOT run VIBSLIDE / PORTAME
        (those live in CNTPLY2 which MULCNTP doesn't call) and does
        NOT advance the pattern row, run HR setup, or emit pre-HR.

        Skips voices whose ARPSCNT is still in $80..$FF — SID-Wizard
        uses ARPSCNT as the "just had STRTSND" detector and bypasses
        multispeed for that voice until ARPSCNT counts down into the
        non-negative range (player.asm line 954: ``lda ARPSCNT,x;
        bmi retsubr``).
        """
        # Same reset as _tick_voice — see comment there.
        v.in_tick2_legato_this_frame = False
        v.adsr_emit_required = False
        if v.instrument is None:
            return
        # ARPSCNT bit-7 set = just-had-STRTSND (counter still in
        # $80..$FF). MULCNTP skips this voice until the next PLAYER
        # tick has decremented + reloaded ARPSCNT into the positive
        # range.
        if v.arp_speed_counter >= 0x80:
            return
        self._tick_wf_table(v)
        if v.instrument.arp_speed & 0x40:
            self._tick_pw_table(v)
        if v.instrument.arp_speed & 0x80:
            self._tick_filt_table(v)
        self._compose_sid_writes(v)
        # Pre-HR window: SID-Wizard's HRGTOFF writes HR-AD/HR-SR at the
        # PLAYER tick (TICK_0/TICK_1) of a new-note row; on MULPLY ticks
        # the SID register simply retains those HR values because MULPLY
        # doesn't write AD/SR. Our compose recomputes AD/SR from the
        # CURRENT instrument's main values each MULPLY tick, which then
        # diff-emits as a wrong write between the PLAYER's HR write and
        # the next PLAYER tick's HR re-assertion. Override sid_ad/sr to
        # the upcoming instrument's HR values while in pre-HR window.
        if v.pending_row is not None and self._pending_row_has_new_note(v):
            ins_hr = self._upcoming_hr_instrument(v, v.pending_row)
            if ins_hr is not None:
                v.sid_ad = ((ins_hr.hr_attack & 0xF) << 4) | (ins_hr.hr_decay & 0xF)
                v.sid_sr = ((ins_hr.hr_sustain & 0xF) << 4) | (ins_hr.hr_release & 0xF)

    def _pending_row_has_new_note(self, v: VoiceState) -> bool:
        """True if ``v.pending_row`` (a row already advanced into by
        TICK_0 but not yet applied) will trigger HR / new-note setup —
        i.e. the SID-Wizard HARDRST -> NEWNOTE path that skips SETPWID /
        FILTPRG. Same exclusion rules as :meth:`_upcoming_row_has_new_note`
        but reading from the stashed row instead of peeking ahead."""
        row = v.pending_row
        if row is None:
            return False
        if row.note is None or row.note >= NOTE_FX_MIN:
            return False
        if row.fx == 0x03:
            return False
        if row.instrument == 0x3F:
            return False
        if v.slide_vib == 0xFF:
            # Note-FX portamento pre-flag: HARDRST takes NONEWNO ->
            # CNTPLAY (no HR, full CNTPLY2 chain). PW/Filter walks
            # DO run at TICK_0/TICK_1.
            return False
        return True

    def _upcoming_row_has_new_note(self, v: VoiceState) -> bool:
        """True if ``v``'s next pattern row will trigger a new note
        (real pitch in the note column) AND go through the HR setup
        path. Used to gate PW / Filter table walks during the pre-HR
        setup window — mirrors SID-Wizard's HARDRST -> NEWNOTE path
        which causes the player to skip SETPWID / FILTPRG and only
        run WFARPTB.

        SID-Wizard's HARDRST (player.asm line 1137+) takes the
        NONEWNO branch — i.e. NO HR setup, just regular continuous
        play — when:
        * the row's note column is empty / >= $60 (no real note), OR
        * the row has portamento big-fx (CURFX2 == $03), OR
        * the row has legato in the instrument column ($3F).

        We mirror those exclusions so the pre-HR window only fires
        on rows that will actually trigger HR.
        """
        if v.pattern is None or v.pattern_row >= len(v.pattern.rows):
            return False
        row = v.pattern.rows[v.pattern_row]
        if row.note is None or row.note >= NOTE_FX_MIN:
            return False
        if row.fx == 0x03:
            # Big-fx $03 = tone-portamento -> NONEWNO branch.
            return False
        if row.instrument == 0x3F:
            # Instrument column $3F = legato -> NONEWNO branch.
            return False
        return True

    # ---- row / sequence advance --------------------------------------

    def _advance_row(self, v: VoiceState) -> None:
        """TICK_0: move to the next pattern row, fetching a new pattern
        from the sequence when the current pattern runs out. Reads the
        row data into ``v.pending_row`` for application at TICK_2 —
        SID-Wizard's TICK_0 reads CURNOT/CURIFX/CURFX2/CURVAL from the
        pattern but does NOT call SEL_INS / STRTSND yet; that happens
        at TICK_2 via STRTSND."""
        if v.pattern is None or v.pattern_row >= len(v.pattern.rows):
            self._advance_sequence(v)
            if v.pattern is None:
                # The sequence reached its terminator with no follow-on
                # pattern. Leave the held note's gate as the last row set
                # it — player.asm's $FE handler does NOT gate off; it just
                # pins the speed counter and keeps running tables. Forcing
                # ptn_gate here would release a still-sustaining note.
                v.pending_row = None
                return
        # READROW/SEQVOLU (player.asm): copy the pending SEQVOLU nibble into
        # MAINVOL (the low nibble of $D418) at the row read, one row after
        # the orderlist MainVolume seq-fx stashed it.
        self.filter_mode_vol = (self.filter_mode_vol & 0xF0) | (self.seqvolu & 0x0F)
        row = v.pattern.rows[v.pattern_row]
        v.pattern_row += 1
        v.pending_row = row
        # SID-Wizard's TICK_0 already reads the row and queues the SID
        # writes for this frame (HRGTOFF emits HR-AD/SR even before
        # SEL_INS), so the voice counts as triggered from TICK_0 of
        # the first non-empty row even if _apply_row hasn't run yet.
        if row.note is not None or row.instrument is not None or row.fx is not None:
            v.triggered = True

    def _advance_sequence(self, v: VoiceState) -> None:
        """Walk the orderlist forward, applying every Transpose /
        TempoOverride encountered, until we find a PlayPattern or run
        into End / Loop.

        A well-formed orderlist always reaches a PlayPattern (or End) within
        one pass, so the loop below returns at the first one. Degenerate
        orderlists — e.g. an empty/unused subtune whose only command is a
        ``Loop`` pointing back at itself with no pattern in the cycle — would
        otherwise spin forever inside a single frame. ``seen`` records each
        position visited in *this* call and breaks the cycle by treating the
        voice as ended; because a real loop returns at its PlayPattern before
        ever revisiting a position, this never triggers for valid tunes. A
        :class:`SubtuneJump` changes which orderlist is being walked, so the
        cycle key includes the orderlist's identity.

        A :class:`SubtuneJump` follows player.asm LOOPSEQ (line 1683): SETSEQA
        repoints only this channel's orderlist at the target subtune, then
        ``lda #0 / tay`` restarts it at position 0, leaving tempo untouched."""
        seen: set = set()
        while v.seq_pos < len(v.sequence):
            key = (id(v.sequence), v.seq_pos)
            if key in seen:
                break
            seen.add(key)
            cmd = v.sequence[v.seq_pos]
            v.seq_pos += 1
            if isinstance(cmd, PlayPattern):
                if 1 <= cmd.pattern <= len(self.swm.patterns):
                    v.pattern = self.swm.patterns[cmd.pattern - 1]
                else:
                    v.pattern = None
                v.pattern_row = 0
                return
            if isinstance(cmd, Transpose):
                # Delayed: store in TRANSP2; _start_note promotes it.
                v.transpose_pending = cmd.semitones
                continue
            if isinstance(cmd, TempoOverride):
                v.tempo_left = cmd.frames_per_row
                v.tempo_right = cmd.frames_per_row
                continue
            if isinstance(cmd, MainVolume):
                # chVolFx: stash into SEQVOLU; _advance_row's READROW copy
                # promotes it to MAINVOL (one-frame-delayed $D418 nibble).
                self.seqvolu = cmd.level & 0x0F
                continue
            if isinstance(cmd, End):
                v.pattern = None
                v.sequence_ended = True
                return
            if isinstance(cmd, Loop):
                v.seq_pos = cmd.position
                continue
            if isinstance(cmd, SubtuneJump):
                target = cmd.subtune * 3 + v.channel
                if target >= len(self.swm.sequences):
                    break
                v.sequence = self.swm.sequences[target]
                v.seq_pos = 0
                continue
            # RawSequenceByte or unknown — skip.
        v.pattern = None
        v.sequence_ended = True

    def _apply_row(self, v: VoiceState, row) -> None:
        """Apply one Row's columns to the voice."""
        note = row.note
        instrument = row.instrument
        fx = row.fx
        fx_value = row.fx_value
        # Whether this row SELECTS an instrument (1..$3E). STRTSND's
        # TABLRST clears the PW/filter reset-off bits when an instrument
        # was just selected (player.asm:1388 ``TABLRST and #$3F``), so a
        # selection always resets those tables; only a same-instrument
        # retrigger honours the instrument's reset-off control bits.
        instrument_selected = instrument is not None and 1 <= instrument <= 0x3E
        # Set when the note column consumed the row's BIGFX03 (tone portamento).
        portamento_applied = False
        # Set when the note column reached STRTSND, which runs INSPTFX after its own
        # register writes (player.asm:2054) -- so the FX columns are deferred past compose.
        started_note = False

        # Any non-empty column wakes the voice up for emit purposes:
        # SID-Wizard's player runs the per-voice loop for any voice
        # whose pattern has reached a real (non-NOP) row, including
        # gate-off / sync / ring / legato rows that don't trigger HR.
        if note is not None or instrument is not None or fx is not None:
            v.triggered = True

        # Instrument column FIRST: a row with both note and instrument
        # is a "play this note with this new instrument" — we want the
        # instrument loaded before _start_note runs (so the WF/PW/Filter
        # table positions reset against the right tables). The pattern
        # processing in SID-Wizard's player follows the same order:
        # instrument-select is resolved, then HR runs against the new
        # instrument's HR slots.
        #
        # Values 1..0x3E select an instrument; 0x3F is legato (no HR);
        # values 0x40..0x7F are small-FX (encoded in the same column as
        # instrument in SWM disk format — SID-Wizard's player CHINSTC
        # reads them into CURIFX, then SEL_INS only writes CURINS when
        # the byte is < maxinstamount). We re-purpose ``instrument`` as
        # ``fx`` for the small-FX range below.
        if instrument is not None and 1 <= instrument <= 0x3E:
            v.instrument_idx = instrument
            if 1 <= instrument <= len(self.swm.instruments):
                ins = self.swm.instruments[instrument - 1]
                v.instrument = ins
                v.octave_shift = ins.octave_shift
        # Inst-column small-FX (CURIFX $40..$7F) applies INDEPENDENTLY
        # of any small-FX in the FX column — player.asm INSPTFX
        # dispatches CURIFX first (SMALPFX) and THEN CURFX2. Apply
        # inst-column small-FX immediately and clear ``instrument`` so
        # the note-column handling below doesn't try to retrigger.
        inst_smallfx = None
        if instrument is not None and 0x40 <= instrument <= 0x7F:
            inst_smallfx = instrument
            instrument = None

        # Note column. _start_note is called exactly once per row, here,
        # ONLY when a real pitch is in the note column. Rows with an
        # instrument change but no note (i.e. legato) do NOT re-trigger.
        # SID-Wizard's HARDRST (player.asm line 1137+) takes the
        # NONEWNO branch — no HR setup, no table reset — when the row
        # has portamento big-fx ($03) or legato instrument ($3F);
        # only the discrete pitch (v.note) updates and the existing
        # WF / PW / Filter table walks continue. Mirror that here.
        if note is not None and note != 0:
            if note < NOTE_FX_MIN:
                if fx == 0x03:
                    # Portamento big-fx: skip HR / table reset and
                    # set up the slide. ``fx_value`` is the portamento
                    # speed (passed as A to SETFMOD). _begin_portamento
                    # seeds vibrato_offset from the previous freq so
                    # the slide starts at the old note and drifts to
                    # the new target.
                    v.note = note
                    self._begin_portamento(v, fx_value if fx_value is not None else 0)
                    portamento_applied = True
                elif v.slide_vib == 0xFF:
                    # Note-FX portamento pre-flag (NPORTAM at $78 set
                    # SLIDEVIB=$FF on the previous row). CLEGATO line
                    # 1370-1371: ``adc SLIDEVIB,x ; bcs SETPRTA`` — when
                    # SLIDEVIB=$FF, the adc overflows and falls into the
                    # portamento setup path. Same TICK_2 behavior as
                    # LEGATO: SETPRTA sets SLIDEVIB=$83 (FREQMODL was
                    # already set by NPORTAM) and LEGATOO -> WRWFGHO.
                    # No STRTSND, no table reset.
                    target_note = max(
                        0,
                        min(
                            len(NOTE_FREQ_LO) - 1,
                            note + v.octave_shift + v.transpose,
                        ),
                    )
                    target_freq = (NOTE_FREQ_HI[target_note] << 8) | NOTE_FREQ_LO[target_note]
                    # SID-Wizard reads ZP FREQLO/HI (pre-carry, pre-detune)
                    # for portamento seeding — see _begin_portamento.
                    previous_freq = (v.zp_freq_hi << 8) | v.zp_freq_lo
                    v.vibrato_offset = (previous_freq - target_freq) & 0xFFFF
                    v.slide_vib = 0x83
                    v.note = note
                elif instrument == 0x3F:
                    # Legato (CURIFX=$3F): SID-Wizard's CHILEGA branch
                    # at player.asm line 1373-1379 sets up a maximal-speed
                    # tone-portamento:
                    #   SETLEGA lda #$7F ; sta FREQMODH,x
                    #   SETPRTA lda #$83 ; sta SLIDEVIB,x
                    # Then LEGATOO runs INSPTFX + WRWFGHO. The next
                    # PORTAME tick slides FREQ instantly to the new
                    # target (FREQMODH=$7F = $7F00 = huge step -> snaps).
                    # We seed vibrato_offset from previous_freq - target
                    # like _begin_portamento does, then set the slide
                    # state directly.
                    target_note = max(
                        0,
                        min(
                            len(NOTE_FREQ_LO) - 1,
                            note + v.octave_shift + v.transpose,
                        ),
                    )
                    target_freq = (NOTE_FREQ_HI[target_note] << 8) | NOTE_FREQ_LO[target_note]
                    # ZP read; see _begin_portamento.
                    previous_freq = (v.zp_freq_hi << 8) | v.zp_freq_lo
                    v.vibrato_offset = (previous_freq - target_freq) & 0xFFFF
                    v.slide_vib = 0x83
                    v.vibrato_freqmod_hi = 0x7F
                    v.note = note
                else:
                    # New pitched note -> start sound.
                    v.note = note
                    self._start_note(v, instrument_selected)
                    started_note = True
            elif note == GATE_OFF_FX:
                # SID-Wizard's NGATEOF (player.asm line 2495) sets
                # PTNGATE=$FE AND immediately AND-masks WFGHOST with
                # $FE — clearing the gate bit on the SID CTRL ghost
                # in the SAME frame, not waiting for the next WF
                # table tick. Critical for chord-heavy instruments
                # paced by arp_speed where many frames may elapse
                # between WF reads. (The gateoff_wf/gateoff_pw
                # pointer branch isn't modelled — all four fixture
                # instruments have those bytes at $00.)
                v.ptn_gate = 0xFE
                v.sid_ctrl &= 0xFE
            elif note == GATE_ON_FX:
                # NGATEON (player.asm line 2489): PTNGATE=$FF AND
                # immediately ORs the gate bit into WFGHOST.
                v.ptn_gate = 0xFF
                v.sid_ctrl |= 0x01
            elif note == SYNC_ON_FX:
                # NSYNCON in player.asm: ``ora #%00000010 ; sta WFGHOST,x``
                v.sid_ctrl |= 0x02
            elif note == SYNC_OFF_FX:
                # NSYNCOF: ``and #%11111101 ; sta WFGHOST,x``
                v.sid_ctrl &= ~0x02 & 0xFF
            elif note == RING_ON_FX:
                # NRINGON: ``ora #%00000100``
                v.sid_ctrl |= 0x04
            elif note == RING_OFF_FX:
                # NRINGOF: ``and #%11111011``
                v.sid_ctrl &= ~0x04 & 0xFF
            elif note == PORTAMENTO_FX:
                # NPORTAM (player.asm ~2473): pre-sign note-FX portamento
                # by setting SLIDEVIB=$FF and FREQMOD via SETFMOD with
                # A=DEFAULTPORTA (= 110 = $6E). The next pitched note's
                # CLEGATO will then take the NONEWNO branch.
                v.slide_vib = 0xFF
                idx = ((110 + 1) >> 1) + ((v.note + v.octave_shift + v.transpose) & 0x7F)
                lo, hi = self._lookup_freqmod(idx)
                v.vibrato_freqmod_lo = lo
                v.vibrato_freqmod_hi = hi
            elif NOTE_FX_MIN <= note < PORTAMENTO_FX and v.instrument is not None:
                # CHVIBFX (player.asm line 2459): CURNOT in $60..$77 takes
                # the vibrato-amp/freq branch which calls VIBAMFX. VIBAMFX
                # does SETINBH (Y=5) -> BIGFX08:
                # SETINBH constructs A = (note_low_nibble << 4) |
                # (instrument_byte_5 & $0F) — i.e. the note's low nibble
                # overrides the instrument vibrato's amp nibble while
                # the freq nibble is kept from the instrument. BIGFX08
                # then runs FORCVIB (SLIDEVIB = instrument.control & $30)
                # and SETVIBR (VIBFREQU / VIBRACNT / FREQMOD from the
                # constructed vibrato byte).
                #
                # The dominant use in PORTAVIBRA_ON=0 builds: terminate
                # an in-progress BIGFX03 portamento by resetting
                # SLIDEVIB back to the instrument's vibrato type — the
                # auto-restore is disabled in that build, so this is
                # the only way the slide ends.
                ins = v.instrument
                self._apply_vibrato_byte(v, ((note & 0x0F) << 4) | (ins.vibrato & 0x0F))
                # Snap any in-progress portamento offset so the slide
                # actually ends here (don't leave a residual offset).
                v.vibrato_offset = 0

        # INSPTFX (player.asm:3179) runs after STRTSND's own AD/SR/WFGHOST writes on a
        # new-note tick, so defer it past this tick's compose; otherwise apply it here.
        if started_note:
            v.deferred_fx = (inst_smallfx, fx, fx_value, portamento_applied)
        else:
            v.deferred_fx = None
            self._apply_fx_columns(v, inst_smallfx, fx, fx_value, portamento_applied)

    def _run_deferred_fx(self, v: VoiceState) -> None:
        """Run a new-note row's INSPTFX, deferred past STRTSND (player.asm:2054)."""
        if v.deferred_fx is None:
            return
        args = v.deferred_fx
        v.deferred_fx = None
        self._apply_fx_columns(v, *args)

    def _apply_fx_columns(
        self,
        v: VoiceState,
        inst_smallfx: Optional[int],
        fx: Optional[int],
        fx_value: Optional[int],
        portamento_applied: bool,
    ) -> None:
        """INSPTFX (player.asm:3179): the inst-column small-FX, then the FX column."""
        # INST_FX dispatches CURIFX ($40..$7F) before PATT_FX reads CURFX2, so if both
        # columns target the same nibble the FX column wins.
        if inst_smallfx is not None:
            self._apply_small_fx(v, inst_smallfx)

        # FX column: PATT_FX (player.asm:3186) sends CURFX2 >= $20 to SMALPFX and $01..$1F through BIGFXTABLE (player.asm:3483); codes this player does not model are reported, never swallowed.
        if fx is not None:
            if fx >= 0x20:
                self._apply_small_fx(v, fx)
            elif fx == 0x01 and fx_value is not None:
                # BIGFX01 (player.asm line 2834): pitch slide UP.
                # ``ldy #$81 ; bne SETSLID`` -> SLIDEVIB=$81, then
                # SETFMOD with A=fx_value. SETFMOD branches at
                # ``beq wrFmodL`` when A=0, storing 0 to both FREQMOD
                # bytes (no slide); otherwise it computes the pitch-
                # dependent amplitude via lsr / adc DPITCH lookup.
                v.slide_vib = 0x81
                if fx_value == 0:
                    v.vibrato_freqmod_lo = 0
                    v.vibrato_freqmod_hi = 0
                else:
                    idx = ((fx_value + 1) >> 1) + ((v.note + v.octave_shift + v.transpose) & 0x7F)
                    lo, hi = self._lookup_freqmod(idx)
                    v.vibrato_freqmod_lo = lo
                    v.vibrato_freqmod_hi = hi
            elif fx == 0x02 and fx_value is not None:
                # BIGFX02 (player.asm line 2838): pitch slide DOWN.
                v.slide_vib = 0x82
                if fx_value == 0:
                    v.vibrato_freqmod_lo = 0
                    v.vibrato_freqmod_hi = 0
                else:
                    idx = ((fx_value + 1) >> 1) + ((v.note + v.octave_shift + v.transpose) & 0x7F)
                    lo, hi = self._lookup_freqmod(idx)
                    v.vibrato_freqmod_lo = lo
                    v.vibrato_freqmod_hi = hi
            elif fx == 0x03 and fx_value is not None and not portamento_applied:
                # BIGFX03 (player.asm:3505) with no new note in the row: SETSLID arms
                # SLIDEVIB=$83 and SETFMOD recomputes the speed against the held DPITCH,
                # so PORTAME keeps sliding toward the note already playing.
                v.slide_vib = 0x83
                idx = ((fx_value + 1) >> 1) + ((v.note + v.octave_shift + v.transpose) & 0x7F)
                v.vibrato_freqmod_lo, v.vibrato_freqmod_hi = self._lookup_freqmod(idx)
            elif fx == 0x04 and fx_value is not None:
                # BIGFX04 = WRITEWF (player.asm:3509): WFGHOST := value, unmasked by PTNGATE.
                v.sid_ctrl = fx_value
            elif fx == 0x05 and fx_value is not None:
                # BIGFX05 = WRITEAD (player.asm:3512): SIDG.AD := value ($D405, no ghost).
                v.sid_ad = fx_value
                v.adsr_emit_required = True
            elif fx == 0x06 and fx_value is not None:
                # BIGFX06 = WRITESR (player.asm:3514): SIDG.SR := value ($D406, no ghost).
                v.sid_sr = fx_value
                v.adsr_emit_required = True
            elif fx == 0x07 and fx_value is not None:
                # BIGFX07 = SMALFX7 (player.asm:3516) selects a chord by its full byte.
                self._select_chord(v, fx_value)
            elif fx == 0x08 and fx_value is not None and v.instrument is not None:
                # BIGFX08 (player.asm:3518): FORCVIB then SETVIBR on the value byte.
                self._apply_vibrato_byte(v, fx_value)
            elif fx == 0x0D and fx_value is not None:
                # BIGFX0D = SETDETU (player.asm:3551): DETUNER := value (signed).
                v.detune = fx_value - 0x100 if fx_value >= 0x80 else fx_value
            elif fx == 0x0E and fx_value is not None:
                # BIGFX0E (player.asm:3558): PWHIGHO := value & $0F.
                v.pw_hi = fx_value & 0x0F
            elif fx == 0x16 and fx_value is not None:
                # BIGFX16 = FORCVI2 (player.asm:3698): SLIDEVIB := value & $30.
                v.slide_vib = fx_value & 0x30
            elif fx == 0x09 and fx_value is not None and v.instrument is not None:
                # BIGFX09 (player.asm:3524): WFTPOS := value*3 + WFTABLEPOS, i.e. WF-table row ``value``.
                target = fx_value * 3
                if target < len(v.instrument.wf_table):
                    v.wf_pos = INST_WF_TABLE_POS + target
            elif fx == 0x0A and fx_value is not None and v.instrument is not None:
                # BIGFX0A (player.asm:3530): PWTPOS := value*3 + the instrument's PW-table base, PWEEPCNT := 0.
                ins = v.instrument
                base = INST_WF_TABLE_POS + len(ins.wf_table) + 1
                if fx_value * 3 < len(ins.pw_table):
                    v.pw_pos = base + fx_value * 3
                    v.pw_sweep_count = 0
            elif fx == 0x0C and fx_value is not None:
                # BIGFX0C = SMALFXC (player.asm:3549): ARPSPED := value, ARPSCNT := $FF.
                v.arp_speed_reload = fx_value
                v.arp_speed_counter = 0xFF
            elif fx == 0x0B and fx_value is not None and v.instrument is not None:
                # BIGFX0B (player.asm line 2880): set filter-table
                # position. FLTPOSI = (fx_value * 3) + byte[$0B] of
                # instrument (= filter-table base offset).
                ins = v.instrument
                target = fx_value * 3
                if target < len(ins.filter_table):
                    v.filt_pos = self._filt_table_start(ins) + target
                    self.filter_sweep_count = 0
            elif fx == 0x0F and fx_value is not None:
                # BIGFX0F (player.asm:3563) writes CTFHGHO, the $D416 cutoff-high ghost.
                self._set_cutoff_hi(fx_value)
            elif 0x17 <= fx <= 0x1C and fx_value is not None:
                # BIGFX1C (player.asm:3706): FLSHIFT := value. BIGFX17..BIGFX1B are bare labels ahead of it (player.asm:3700-3704), so those codes run the same shift.
                self.filter_shift = fx_value
            elif fx == 0x1F and fx_value is not None:
                # BIGFX1F (player.asm:3758) stores the value's nibbles into FSWITCH and RESONIB, so it also clears the routing bits until the next STRTSND re-ORs them; without FILT_CTRL_FX_ON the whole body is gone and the label is the following ``rts``.
                if self.features.filt_ctrl_fx:
                    self.filter_switch = fx_value & 0x0F
                    self.filter_resonance = fx_value & 0xF0
            elif fx == 0x10 and fx_value is not None:
                # BIGFX10 (player.asm:3586): main single tempo -- ``ora #$80`` pins the
                # funk cycle to one slot, and MAINTMP resets every track's TMPPOS.
                self._set_tempo(self.voices, fx_value & 0x7F, fx_value & 0x7F)
            elif fx == 0x11 and fx_value is not None:
                # BIGFX11 (player.asm:3627): main funktempo, high nibble then low.
                self._set_tempo(self.voices, fx_value >> 4, fx_value & 0x0F)
            elif fx == 0x13 and fx_value is not None:
                # BIGFX13 (player.asm:3658): track single tempo, this voice only.
                self._set_tempo([v], fx_value & 0x7F, fx_value & 0x7F)
            elif fx == 0x14 and fx_value is not None:
                # BIGFX14 (player.asm:3669): track funktempo, this voice only.
                self._set_tempo([v], fx_value >> 4, fx_value & 0x0F)
            elif fx in (0x1D, 0x1E) and not self.features.delaysupport:
                # BIGFX1D / BIGFX1E (player.asm:3730 / 3746) are wrapped in
                # ``.if feature.DELAYSUPPORT_ON``, which only the Extra player sets
                # (altplayers.inc:553); elsewhere both fall through to a bare ``rts``.
                pass
            elif not portamento_applied:
                self._report_unsupported(v, "big-fx", fx)

    @staticmethod
    def _live_nibble(v: VoiceState, which: str) -> Optional[int]:
        """The voice's current ADSR nibble: its override, else the instrument's."""
        override = getattr(v, which + "_override")
        if override is not None:
            return override
        return getattr(v.instrument, which) & 0xF if v.instrument is not None else 0

    def _apply_small_fx(self, v: VoiceState, code: int) -> None:
        """Dispatch a single SMALFX2..SMALFX7 byte (player.asm
        ``INDEXJ2`` table at line 2605). Used for small-fx in both the
        inst column ($40..$7F) and the fx column ($20..$7F).

        With SID-Wizard's default ``ALLGHOSTREGS_ON=0`` for single-SID
        builds, SMALFX2/3/5/6 each write a full AD/SR byte by taking
        the OTHER nibble from the INSTRUMENT'S main value (not from a
        previous SMALFX or ghost register). This means two SMALFX
        targeting the same SID register in one row don't combine —
        the later one overwrites the earlier. We model that by
        setting the modified nibble's override and CLEARING the other
        nibble's override (so compose falls back to ``ins.*``).
        """
        high = code & 0xF0
        low = code & 0x0F
        # ALLGHOSTREGS_ON (player.asm:3278-3315) sources the untouched nibble from the live SIDG.AD/SR ghost rather than the instrument, so SMALFX combine.
        keep = self.features.allghostregs
        if high == 0x20:
            v.attack_override = low
            v.decay_override = self._live_nibble(v, "decay") if keep else None
        elif high == 0x30:
            v.attack_override = self._live_nibble(v, "attack") if keep else None
            v.decay_override = low
        elif high == 0x50:
            v.sustain_override = low
            v.release_override = self._live_nibble(v, "release") if keep else None
        elif high == 0x60:
            v.sustain_override = self._live_nibble(v, "sustain") if keep else None
            v.release_override = low
        # SMALFX2/3/5/6 write SIDG.AD / SIDG.SR directly, taking the OTHER nibble from the current instrument (= what the overrides above model).
        ins = v.instrument
        if high in (0x20, 0x30, 0x50, 0x60):
            if ins is not None:
                attack = v.attack_override if v.attack_override is not None else ins.attack
                decay = v.decay_override if v.decay_override is not None else ins.decay
                sustain = v.sustain_override if v.sustain_override is not None else ins.sustain
                release = v.release_override if v.release_override is not None else ins.release
                v.sid_ad = ((attack & 0xF) << 4) | (decay & 0xF)
                v.sid_sr = ((sustain & 0xF) << 4) | (release & 0xF)
                v.adsr_emit_required = True
        elif high == 0x40:
            # SMALFX4 (player.asm:3298) merges the value nibble into WFGHOST's high nibble.
            v.sid_ctrl = (low << 4) | (v.sid_ctrl & 0x0F)
        elif high == 0xE0:
            # SMALFXE (player.asm:3407) merges a low nibble but never stores it -- its ``jmp WRITEWF`` is commented out in 1.94, so it is a no-op.
            pass
        elif high == 0x70:
            self._select_chord(v, low)
        elif high == 0xC0:
            # SMALFXC (player.asm:3391): ARPSPED := nibble, ARPSCNT := $FF.
            v.arp_speed_reload = low
            v.arp_speed_counter = 0xFF
        elif high == 0x80:
            # SMALFX8 = VIBAMFX (player.asm:3338): the nibble overrides the instrument vibrato byte's amplitude, then BIGFX08.
            if ins is not None:
                self._apply_vibrato_byte(v, (low << 4) | (ins.vibrato & 0x0F))
        elif high == 0x90:
            # SMALFX9 (player.asm:3349): VIBFREQU := nibble * 2, leaving amplitude alone.
            v.vibrato_period = low << 1
        elif high == 0xD0:
            # SMALFXD (player.asm:3399): DETUNER := nibble * 8 (three ASLs into SETDETU).
            v.detune = (low << 3) & 0xFF
        elif high == 0xB0:
            # SMALFXB (player.asm:3377) writes FLTBAND, the $D418 high nibble.
            self.filter_mode_vol = (self.filter_mode_vol & 0x0F) | (low << 4)
        elif high == 0xF0:
            # SMALFXF (player.asm:3418) writes RESONIB, the $D417 high nibble.
            self.filter_resonance = low << 4
        elif high == 0xA0:
            # SMALFXA (player.asm:3356) writes MAINVOL and SEQVOLU with the value nibble.
            self.filter_mode_vol = (self.filter_mode_vol & 0xF0) | low
            self.seqvolu = low
        else:
            self._report_unsupported(v, "small-fx", code)

    def _set_tempo(self, voices: List[VoiceState], left: int, right: int) -> None:
        """Write a tempo pair and rewind TMPPOS to its base (player.asm:3591-3625)."""
        for voice in voices:
            voice.tempo_left = left
            voice.tempo_right = right
            voice.tempo_toggle = 0

    def _apply_vibrato_byte(self, v: VoiceState, vibrato_byte: int) -> None:
        """BIGFX08 (player.asm:3518): FORCVIB then SETVIBR (player.asm:2894).

        FORCVIB re-reads the vibrato type from the instrument control byte;
        SETVIBR derives VIBFREQU / VIBRACNT and the FREQMOD amplitude, which
        SETFMOD leaves at zero for an amplitude nibble of 0 (``beq wrFmodL``).
        """
        ins = v.instrument
        v.slide_vib = ins.control & 0x30 if ins is not None else 0
        freq_nibble = vibrato_byte & 0x0F
        amp_nibble = (vibrato_byte >> 4) & 0x0F
        v.vibrato_period = freq_nibble * 2
        if v.slide_vib == 0x30:
            v.vibrato_freq_counter = 0
        elif v.slide_vib >= 0x20:
            v.vibrato_freq_counter = freq_nibble
        else:
            v.vibrato_freq_counter = freq_nibble >> 1
        if amp_nibble == 0:
            v.vibrato_freqmod_lo = 0
            v.vibrato_freqmod_hi = 0
            return
        idx = (amp_nibble << 2) + ((v.note + v.octave_shift + v.transpose) & 0x7F)
        v.vibrato_freqmod_lo, v.vibrato_freqmod_hi = self._lookup_freqmod(idx)

    def _select_chord(self, v: VoiceState, chord: int) -> None:
        """SMALFX7 (player.asm:3325): CURCHORD := chord, CHORDPOS := CHDPTRLO[chord]."""
        if 1 <= chord <= len(self._chord_starts):
            v.current_chord = chord
            v.chord_pos = self._chord_starts[chord - 1]

    def _set_cutoff_hi(self, value: int) -> None:
        """Write CTFH_GHO (player.asm:2179), the single global cutoff-high ghost."""
        self.filter_cutoff_hi = value & 0xFF
        if self.filter_controller_voice is not None:
            self.filter_controller_voice.filt_hi = value & 0xFF

    def _set_filter_routing(self, v: VoiceState, filtered: bool) -> None:
        """SETFLTP (player.asm:1999-2008): OR / AND this voice's FSWITCH routing bit."""
        bit = 1 << next((i for i, sv in enumerate(self.voices) if sv is v), 3)
        if filtered:
            self.filter_switch |= bit
        else:
            self.filter_switch &= ~bit & 0xFF

    def _report_unsupported(self, v: VoiceState, column: str, code: int) -> None:
        """Record + warn (once per ``(column, code)``) that ``code`` is unmodelled."""
        key = (column, code)
        seen = self._unsupported_effects.get(key, 0)
        self._unsupported_effects[key] = seen + 1
        if not seen:
            voice_idx = next((i for i, sv in enumerate(self.voices) if sv is v), -1)
            warnings.warn(
                SWMUnsupportedEffectWarning(column, code, self.frame_idx, voice_idx),
                stacklevel=2,
            )

    @property
    def unsupported_effects(self) -> dict[tuple[str, int], int]:
        """Per-``(column, code)`` counts of the effects this render did not model."""
        return dict(self._unsupported_effects)

    def _start_note(self, v: VoiceState, instrument_selected: bool = False) -> None:
        """Re-trigger the voice: reset gates / tables / hard-restart."""
        # Promote any pending orderlist transpose now (TRANSP2 -> TRANSP):
        # the change takes effect with this new note, not on the previous
        # note's held tail during the pre-trigger HR frames.
        v.transpose = v.transpose_pending
        v.ptn_gate = 0xFF
        # SID-Wizard runs the HR test-bit sequence on every note
        # trigger (including the first one on a fresh F1 playback).
        v.hr_timer = HR_FRAMES
        v.note_played = True
        v.triggered = True
        # Per-note ADSR overrides from small-fx $2x..$6x last for the
        # duration of that note only.
        v.attack_override = None
        v.decay_override = None
        v.sustain_override = None
        v.release_override = None
        if v.instrument is not None:
            # SETWFTP (player.asm:1853): ``lda #WFTABLEPOS ; sta WFTPOS,x``
            # — an absolute offset from the instrument base, not row 0.
            v.wf_pos = INST_WF_TABLE_POS
            # STRTSND's TABLRST: when an instrument is selected this row,
            # the control byte is AND-ed with $3F, clearing the PW/filter
            # reset-off bits so both tables reset. Without a selection the
            # instrument's own bit 6 (pulseresetOFF) / bit 7 (filtresetOFF)
            # are honoured — a same-instrument retrigger then keeps the
            # PW / filter walk running instead of restarting it.
            ctrl = v.instrument.control
            pw_reset_off = (not instrument_selected) and (ctrl & 0x40)
            filt_reset_off = (not instrument_selected) and (ctrl & 0x80)
            if not pw_reset_off:
                # PWTPOS resets to the PW-table START as an ABSOLUTE offset
                # from the instrument base (STRTSND ~line 1445 reads the
                # header's PW-table pointer). _tick_pw_table walks from here.
                v.pw_pos = INST_WF_TABLE_POS + len(v.instrument.wf_table) + 1
            if not filt_reset_off:
                v.filt_pos = self._filt_table_start(v.instrument)
            v.wf_arp_pitch = 0
            v.wf_arp_absolute = False
            v.wf_speed_counter = 0
            # STRTSND (player.asm line 1422) stores $FF to ARPSCNT so
            # the very first post-note WFARPTB pass falls through the
            # BPL check and reloads from ARPSPED.
            v.arp_speed_counter = 0xFF
            # Latch ARPSPED for the WF tick's reload + multispeed flags.
            v.arp_speed_reload = v.instrument.arp_speed
            v.detune = 0
            # PWEEPCNT / CWEPCNT / PKBDTRK are NOT cleared by STRTSND in
            # player.asm: they're written only by their respective table
            # walks (PWSWEEP / SEPWPOS / PWADVAN for PW; SETFILT row
            # advance / BIGFX0B for CWEPCNT). They persist across note
            # triggers with whatever the previous walk left them at.
            # Clearing them here drifted the post-STRTSND walks.
            #
            # STRTSND's SETFLTP block (player.asm line 1553-1568) sets
            # FSWITCH (= our voice_in_filter) on the SAME tick: voice is
            # filtered if the new instrument's filter table starts with
            # anything other than $FF; CLEARED if it starts with $FF
            # (= "instrument not filtered" marker) or is empty. This is
            # the ONLY place SID-Wizard updates FSWITCH per voice; the
            # CNTPLY2 FILTPRG walk doesn't touch it.
            #
            # SETFLTP also conditionally REASSIGNS FLTCTRL (= the global
            # filter-controller voice): for ANY ft[0] != $00, $FF this
            # voice claims controllership (sweep rows $01..$7F and
            # set-mode rows $80..$FE alike). ft[0] == $00 leaves FLTCTRL
            # alone (= "filtered but doesn't control"); ft[0] == $FF
            # clears the routing bit and relinquishes control iff this
            # voice WAS the controller.
            ft = v.instrument.filter_table
            v.voice_in_filter = bool(ft) and ft[0] != FILT_TABLE_END
            self._set_filter_routing(v, v.voice_in_filter)
            if ft and ft[0] not in (0x00, FILT_TABLE_END):
                self.filter_controller_voice = v
            elif ft and ft[0] == FILT_TABLE_END:
                if self.filter_controller_voice is v:
                    self.filter_controller_voice = None
            # Seed chord state from the instrument's default-chord field
            # (1-based; 0 means "no chord"). The WF-table arp tick will
            # consume ``chord_pos`` whenever it hits a ``$7F`` arp byte.
            chord_idx = v.instrument.default_chord
            if 1 <= chord_idx <= len(self._chord_starts):
                v.current_chord = chord_idx
                v.chord_pos = self._chord_starts[chord_idx - 1]
            else:
                v.current_chord = 0
                v.chord_pos = 0
            self._setup_vibrato(v)

    # ---- vibrato setup / tick ---------------------------------------

    def _setup_vibrato(self, v: VoiceState) -> None:
        """Initialise per-voice vibrato state from the instrument's
        ``vibrato`` and ``vibrato_delay`` bytes — mirrors player.asm's
        SETVIB0 / SETVIBR (line ~2290).

        We model NORMVIB (SLIDEVIB=$10, control bits 4-5 = 01) and
        INCVIBR (SLIDEVIB=$00, bits = 00). INCVIBR shares this setup:
        FREQMOD starts at the amp-nibble value (0 for the common
        ramp-from-silence case) and grows each frame in _tick_vibrato.
        The delayed-slide variants ($20/$30) are still out of scope.

        FREQMOD is the calculated-vibrato variant (CALCVIBRATO_ON in upstream
        settings.cfg): index = ``(amp << 2) + play_note``, looked up through
        :meth:`_lookup_freqmod` against :data:`EXPTABH`.
        """
        ins = v.instrument
        v.slide_vib = ins.control & 0x30 if ins is not None else 0
        v.vibrato_offset = 0
        if ins is None:
            v.vibrato_delay_counter = 0
            v.vibrato_period = 0
            v.vibrato_freq_counter = 0
            v.vibrato_freqmod_lo = 0
            v.vibrato_freqmod_hi = 0
            return
        v.vibrato_delay_counter = ins.vibrato_delay & 0x7F
        freq_nibble = ins.vibrato & 0x0F
        amp_nibble = (ins.vibrato >> 4) & 0xF
        v.vibrato_period = freq_nibble * 2
        # Initial VIBRACNT for SLIDEVIB=$10: ``freq_nibble / 2`` —
        # player.asm SETVIBR (line 2295) shifts the freq nibble down
        # once for SLIDEVIB >= $20 and a second time for SLIDEVIB <
        # $20 (the NORMVIB quarter-timer path).
        v.vibrato_freq_counter = freq_nibble >> 1
        # Calculated-vibrato FREQMOD lookup. Player.asm SETVAMP does
        # ``and #$F0; lsr`` -> A = amp_nibble*8; SETFMOD's ``beq wrFmodL``
        # then zeroes FREQMOD when amp_nibble == 0 (no lookup happens).
        # Mirror that — earlier code unconditionally looked up
        # NOTE_FREQ[play_note], producing a phantom FREQMOD on
        # vibrato-disabled instruments (vibrato byte $00). Hidden until
        # the VIBRACNT bail on vibrato_period==0 was removed.
        if amp_nibble == 0:
            v.vibrato_freqmod_lo = 0
            v.vibrato_freqmod_hi = 0
        else:
            play_note = (v.note + v.octave_shift + v.transpose) & 0x7F
            lo, hi = self._lookup_freqmod((amp_nibble << 2) + play_note)
            v.vibrato_freqmod_lo = lo
            v.vibrato_freqmod_hi = hi

    @staticmethod
    def _lookup_freqmod(index: int) -> tuple[int, int]:
        """LOOKUPA / CHKTEND / calcVib (player.asm:2925-2940): FREQMOD from ``index``.

        Below :data:`EXP_THRESHOLD` the fine half is ``EXPTABH[index]`` with a zero
        high byte; at or above it the rough half is the ``FREQTBL`` / ``FREQTBH``
        pair at ``index - EXP_THRESHOLD``, with MAXSLID clamping the index.
        """
        y = min(index & 0xFF, EXP_MAX_INDEX)
        if y < EXP_THRESHOLD:
            return EXPTABH[y], 0
        i = y - EXP_THRESHOLD
        if i < len(NOTE_FREQ_LO):
            return NOTE_FREQ_LO[i], NOTE_FREQ_HI[i]
        # MAXSLID's clamp lands one byte PAST FREQTBL / FREQTBH (player.asm:2936).
        return FREQTBL_PAST_END, EXPTABH[EXPTABH_FREQTBH_OFFSET + i]

    def _tick_vibrato(self, v: VoiceState) -> None:
        """Advance vibrato OR tone-portamento by one frame.

        Handles two slide_vib modes:
        * NORMVIB ($10): delay-then-triangle vibrato as in player.asm
          NORMVIB + DOVIBRA. Adds / subtracts FREQMOD from a running
          ``vibrato_offset`` that ``_compose_sid_writes`` folds into
          the base freq.
        * Tone portamento ($83, set by big-fx 03): drift the freq
          offset toward 0 by FREQMOD each frame, mirroring
          player.asm's PORTAME path (line ~1650). The offset was
          seeded to ``previous_freq - target_freq`` at the portamento
          row trigger so freq starts at the previous note and slides
          to the target. When the offset crosses 0 the portamento
          terminates (slide_vib reverts to the instrument's vibrato
          type and vibrato state is re-initialised via SETVIB1).

        Skipped when there is no instrument loaded or when the
        slide_vib type isn't one of the above.
        """
        if v.instrument is None:
            return
        if v.slide_vib == 0x83:
            self._tick_portamento(v)
            return
        if v.slide_vib == 0x81:
            # SLIDES path with SLIDEVIB=$81 (player.asm line 1644):
            # ADDFREQ — FREQLO += FREQMODL, FREQHI += FREQMODH. We
            # model this as vibrato_offset += FREQMOD (compose folds
            # the offset into the base freq).
            mod = (v.vibrato_freqmod_hi << 8) | v.vibrato_freqmod_lo
            v.vibrato_offset = (v.vibrato_offset + mod) & 0xFFFF
            return
        if v.slide_vib == 0x82:
            # SLIDES path with SLIDEVIB=$82 (player.asm line 1642):
            # SUBFREQ — FREQLO -= FREQMODL.
            mod = (v.vibrato_freqmod_hi << 8) | v.vibrato_freqmod_lo
            v.vibrato_offset = (v.vibrato_offset - mod) & 0xFFFF
            return
        if v.slide_vib == 0x00:
            # INCVIBR (player.asm line 1688): the FREQMOD amplitude grows
            # each frame by VIDELCNT (the vibrato-delay byte, reused as
            # the increase ratio — never decremented here), then falls
            # straight through to the shared DOVIBRA triangle. No delay
            # phase. With FREQMOD seeded to 0 in _setup_vibrato this
            # ramps the vibrato depth up from silence.
            mod = ((v.vibrato_freqmod_hi << 8) | v.vibrato_freqmod_lo) + v.vibrato_delay_counter
            v.vibrato_freqmod_lo = mod & 0xFF
            v.vibrato_freqmod_hi = (mod >> 8) & 0xFF
        elif v.slide_vib == 0x10:
            if v.vibrato_delay_counter >= 0:
                v.vibrato_delay_counter -= 1
                return
        else:
            return  # only INCVIBR / NORMVIB triangles supported beyond this
        # DOVIBRA: decrement freq counter (reloading from VIBFREQU on
        # wrap), then choose ADD or SUB based on which half we're in.
        # Mirrors player.asm DOVIBRA exactly — including the case where
        # VIBFREQU == 0 (vibrato byte $00): the counter still cycles
        # through every 8-bit value, FREQMOD is 0 so the offset doesn't
        # actually move, but the counter advance is observable in the
        # ghost dump.
        if v.vibrato_freq_counter == 0:
            v.vibrato_freq_counter = v.vibrato_period
        v.vibrato_freq_counter = (v.vibrato_freq_counter - 1) & 0xFF
        mod = (v.vibrato_freqmod_hi << 8) | v.vibrato_freqmod_lo
        # `asl A ; cmp VIBFREQU ; bcc ADDFREQ` — 8-bit asl truncates,
        # bcc takes the ADD branch iff (VIBRACNT << 1) & $FF < VIBFREQU.
        asl_result = (v.vibrato_freq_counter << 1) & 0xFF
        if asl_result < v.vibrato_period:
            v.vibrato_offset = (v.vibrato_offset + mod) & 0xFFFF
        else:
            v.vibrato_offset = (v.vibrato_offset - mod) & 0xFFFF

    def _tick_portamento(self, v: VoiceState) -> None:
        """Drift ``vibrato_offset`` toward 0 by FREQMOD each frame.

        At the portamento row trigger the offset was seeded to
        ``previous_freq - target_freq`` (signed 16-bit, two's
        complement). Each tick we move it one FREQMOD step closer to
        zero; when we cross zero, snap to 0.

        SID-Wizard 1.94's default ``PORTAVIBRA_ON=0`` build does NOT
        terminate the slide when target is reached — PORTEND just
        snaps FREQ to target via TARGETN and SLIDEVIB stays $83 until
        either a CHVIBFX note-FX (CURNOT in $60..$77) calls FORCVIB or
        the next pitched note's STRTSND overwrites SLIDEVIB. We mirror
        that by snapping ``vibrato_offset`` to 0 but leaving
        ``slide_vib`` / ``vibrato_*`` untouched.
        """
        offset = v.vibrato_offset
        # Interpret as signed 16-bit.
        signed = offset - 0x10000 if offset & 0x8000 else offset
        if signed == 0:
            return
        # SID-Wizard's PORTAUP/PORTADN paths use SUBTLY DIFFERENT
        # effective deltas: snap when ``|diff| < FREQMOD`` (UP) /
        # ``|diff| <= FREQMOD`` (DOWN) — bare ``mod`` in both
        # conditions — but advance ZP by ``FREQMOD + 1`` because
        # ADDFREQ / SUBFREQ inherit a carry from the preceding sbc.
        # That +1 asymmetry makes the slide overshoot the target by
        # one freq unit before snapping. Conflating the two
        # ("step = mod + 1" everywhere) snaps one frame too early on
        # the boundary case ``|signed| == FREQMOD``.
        mod = (v.vibrato_freqmod_hi << 8) | v.vibrato_freqmod_lo
        step = mod + 1
        if signed > 0:
            # DOWN: ASM snaps when |signed| <= FREQMOD (inclusive).
            if signed <= mod:
                v.vibrato_offset = 0
                return
            new_signed = signed - step
        else:
            # UP: ASM snaps when |signed| < FREQMOD (strict). At
            # signed = -FREQMOD the advance fires, landing at target+1;
            # the next tick takes the DOWN path and snaps.
            if signed + mod > 0:
                v.vibrato_offset = 0
                return
            new_signed = signed + step
        v.vibrato_offset = new_signed & 0xFFFF

    def _begin_portamento(self, v: VoiceState, fx_value: int) -> None:
        """Start tone portamento (big-fx $03) toward the current
        ``v.note`` target. Mirrors player.asm BIGFX03 -> SETSLID ->
        SETFMOD plus the SLIDEVIB=$83 marker.

        ``fx_value`` is the row's big-fx value (= portamento speed
        passed as ``A`` to SETFMOD). The FREQMOD lookup uses
        ``(fx_value >> 1) + target_pitch`` per the calculated-slide
        path in SETFMOD (LSR then ADC DPITCH).

        ``vibrato_offset`` is set to ``previous_freq - target_freq``
        so when ``_compose_sid_writes`` folds it into the
        target-note base freq we get the previous-note freq as the
        starting point of the slide.
        """
        # Target freq: the row's note column with octave + transpose
        # (= the play note we just stored in v.note).
        target_note = max(0, min(len(NOTE_FREQ_LO) - 1, v.note + v.octave_shift + v.transpose))
        target_freq = (NOTE_FREQ_HI[target_note] << 8) | NOTE_FREQ_LO[target_note]
        # SID-Wizard's portamento seed reads ZP FREQLO/FREQHI (= the
        # pre-carry, pre-detune ghost) — that's the value the prior
        # frame's WF arp / vibrato / portamento step left there.
        previous_freq = (v.zp_freq_hi << 8) | v.zp_freq_lo
        v.vibrato_offset = (previous_freq - target_freq) & 0xFFFF
        v.slide_vib = 0x83
        # SETFMOD index: ``lsr A ; adc DPITCH,x`` — the lsr sets carry to
        # bit 0 of fx_value, then adc adds DPITCH plus that carry.
        # Effective index = ceil(fx_value / 2) + DPITCH, NOT floor.
        # For odd fx_value this differs from a naive ``fx_value >> 1``.
        idx = ((fx_value + 1) >> 1) + (target_note & 0x7F)
        lo, hi = self._lookup_freqmod(idx)
        v.vibrato_freqmod_lo = lo
        v.vibrato_freqmod_hi = hi

    # ---- HR pre-write lookahead --------------------------------------

    def _upcoming_hr_instrument(self, v: VoiceState, row: Any) -> Optional[Instrument]:
        """Return the instrument whose HR will fire on the upcoming row's
        trigger, or None if HR won't fire. ``row`` is ``v.pending_row``.
        """
        if row is None:
            return None
        if row.note is None or row.note >= NOTE_FX_MIN:
            return None
        if row.fx == 0x03:
            return None
        if row.instrument == 0x3F:
            return None
        if v.slide_vib == 0xFF:
            # Note-FX portamento pre-flag (NPORTAM on previous row).
            # HARDRST line 1141-1143 takes NONEWNO when SLIDEVIB=$FF
            # (``ldy SLIDEVIB,x ; iny ; beq NONEWNO``) — HR doesn't fire.
            return None
        ins = v.instrument
        if (
            row.instrument is not None
            and 1 <= row.instrument <= 0x3E
            and 1 <= row.instrument <= len(self.swm.instruments)
        ):
            ins = self.swm.instruments[row.instrument - 1]
        return ins

    def _apply_tick(self, tempo: int) -> int:
        """Which SPDCNT tick applies the row read at TICK_0 (= runs TICK_2).

        FASTSPEEDBIND_ON (player.asm:1496-1514, 1704-1723) short-circuits
        CHKTMP1 to PTN_SEQ when the tempo is below 2 and CHKTMP2 to TICK_2 when
        it is below 3, so tempo 1 collapses all three ticks into TICK_0 and
        tempo 2 collapses TICK_1 + TICK_2. Without the flag TICK_2 is only ever
        reached at SPDCNT 2, which is why tempo 1-2 makes no sound.
        """
        if self.features.fastspeedbind:
            return min(2, max(0, tempo - 1))
        return 2

    def _hr_fires_at_tick(self, ins: Instrument, pre_incr_sc: int) -> bool:
        """Whether HRGTOFF fires at this pre-HR tick. SID-Wizard's
        HARDRST passes A=$02 at TICK_0 (= pre_incr_sc 0, masks control
        bit 1) and A=$01 at TICK_1 (= pre_incr_sc 1, masks control
        bit 0). The result of ``control & A`` decides whether HRGTOFF
        actually runs that tick.
        """
        if not self.features.hardrestypes:
            # Flag off: HARDRST has no HR-tick-timer and falls straight into HRGTOFF, so HR fires on both pre-HR ticks.
            return pre_incr_sc in (0, 1)
        if pre_incr_sc == 0:
            return bool(ins.control & 0x02)
        if pre_incr_sc == 1:
            return bool(ins.control & 0x01)
        return False

    def _maybe_emit_pre_hr(self, v: VoiceState, row: Any, pre_incr_sc: int) -> None:
        """SID-Wizard HRGTOFF (TICK_0/TICK_1 HR write): write the
        upcoming row's instrument HR-AD/HR-SR to ``v.sid_ad/sr`` and
        AND-mask the gate bit off CTRL.

        HR-AD/SR are written on both pre-HR ticks (TICK_0 and TICK_1)
        so the values persist through the pre-trigger window even when
        only one of those ticks actually fires HRGTOFF in the asm
        (dedup at SID-emit time hides the duplicate). The per-tick
        gate-off / $18-staccato CTRL override only fires when the
        ASM's HRGTOFF actually runs (control bit 1 for TICK_0, bit 0
        for TICK_1) -- a $1A instrument fires HRGTOFF only at TICK_0,
        a $1D instrument only at TICK_1.

        For staccato instruments (bit 2 set), the HR fire overrides
        CTRL with $18 instead of the gate-off-WF value -- per spec
        §7's ``lda #$18; sta SIDG.WAVE,x; rts``. Caller skips
        WF/PW/Filter/Vibrato walks via ``_is_staccato_hr_tick``.
        """
        ins = self._upcoming_hr_instrument(v, row)
        if ins is None:
            return
        if not self._hr_fires_at_tick(ins, pre_incr_sc):
            return
        v.ptn_gate = 0xFE  # PTNGATE write is inside HRGTOFF (player.asm 1568)
        # SID-Wizard's HARDRST gates the entire HR-AD/SR/PTNGATE/CTRL
        # write block on ``A & control != 0`` (lines 1167-1170). For
        # instruments with bit 0 only (HR fires at TICK_1) or bit 1
        # only (HR fires at TICK_0), the non-fire tick must not touch
        # AD/SR — the SID retains the previous note's values until the
        # fire tick. Compose's normal AD/SR write is gated separately
        # via ``in_new_note_setup`` to avoid clobbering this.
        if self.features.hardrestypes:
            v.sid_ad = ((ins.hr_attack & 0xF) << 4) | (ins.hr_decay & 0xF)
            v.sid_sr = ((ins.hr_sustain & 0xF) << 4) | (ins.hr_release & 0xF)
        else:
            # defauHR (player.asm:1588): DEFAULTHRADSR = $0F00 for every instrument.
            v.sid_ad = DEFAULT_HR_ADSR >> 8
            v.sid_sr = DEFAULT_HR_ADSR & 0xFF
        v.adsr_emit_required = True
        if self.features.hardrestypes and ins.control & 0x04:
            # Staccato exit per spec §7: WFGHOST = $18.
            v.sid_ctrl = 0x18
        else:
            # Normal HR: gate-off the current WF byte.
            v.sid_ctrl = v.sid_ctrl & 0xFE

    def _is_staccato_hr_tick(self, v: VoiceState, pre_incr_sc: int, apply_sc: int = 2) -> bool:
        """True if this is a pre-HR tick where staccato HRGTOFF fires
        (= rts before tables walk). _tick_voice uses this to skip the
        WF/vibrato/PW/Filter ticks for the staccato exit.
        """
        if pre_incr_sc >= apply_sc:
            return False
        if v.pending_row is None:
            return False
        ins = self._upcoming_hr_instrument(v, v.pending_row)
        if ins is None:
            return False
        if not self.features.hardrestypes or not (ins.control & 0x04):
            return False
        return self._hr_fires_at_tick(ins, pre_incr_sc)

    # ---- chord-table tick --------------------------------------------

    def _tick_chord(self, v: VoiceState) -> bool:
        """Pull the next pitch out of the chord table for ``v``.

        Triggered when the WF table's arp byte is ``$7F``. Follows the
        player.asm chord routine (around ``p_chdt1`` / ``LOOPCHD``):

        * ``$7E`` — "return from chord to WFARP table". Reset
          ``chord_pos`` for the next trigger and signal the caller to
          skip past this WF row and re-process the next row in the
          same frame (matching the ``jmp RDWFROW`` in player.asm).
        * ``$7F`` — "loop chord". Reset ``chord_pos`` to the chord
          start and read the FIRST pitch of the chord.
        * Otherwise — use the byte as a relative-up pitch offset added
          to the row's note.

        Returns ``True`` if a ``$7E`` was hit (caller should jump to
        the next WF row); ``False`` if a pitch was applied normally.
        """
        if v.current_chord == 0:
            return False
        table = self._chord_table
        if not (1 <= v.current_chord <= len(self._chord_starts)):
            # CURCHORD is out of bounds (e.g. euphoria's TOMDRUM has
            # default_chord=$31 with only 15 chords parsed). The real
            # player reads CHDPTRLO past the end of the in-memory
            # table; we can't replicate that exact OOB read, but we
            # also know these instruments don't actually have $7F arp
            # bytes, so this branch protects against the IndexError.
            return False
        chord_start = self._chord_starts[v.current_chord - 1]
        if v.chord_pos >= len(table):
            v.chord_pos = chord_start
        byte = table[v.chord_pos]
        if byte == CHORD_END_RETURN:
            v.chord_pos = chord_start
            return True
        if byte == CHORD_END_LOOP:
            v.chord_pos = chord_start
            if v.chord_pos < len(table):
                byte = table[v.chord_pos]
        v.wf_arp_pitch = byte
        v.wf_arp_absolute = False
        # PLYCHRD's normal-pitch path falls through to RELPTCH which
        # overwrites FREQLO/FREQHI ZP, wiping any accumulated vibrato
        # mutation (same as ABSPTCH/RELPTCH in _tick_wf_table). Mirror
        # that so our compose doesn't carry stale vibrato over a chord
        # pitch write.
        v.vibrato_offset = 0
        v.wf_did_pitch_write = True
        v.chord_pos += 1
        return False

    # ---- instrument WF / PW / Filter table ticks ---------------------

    def _image(self, ins: Instrument) -> bytes:
        """The instrument's memory image, cached per instrument object."""
        image = self._inst_images.get(id(ins))
        if image is None:
            image = ins.memory_image()
            self._inst_images[id(ins)] = image
        return image

    @staticmethod
    def _filt_table_start(ins: Instrument) -> int:
        """Absolute offset of ``ins``' filter table (its header byte $0B)."""
        return INST_WF_TABLE_POS + len(ins.wf_table) + 1 + len(ins.pw_table) + 1

    def _tick_wf_table(self, v: VoiceState) -> None:
        """Advance the per-frame WF / arp / detune table by one step.

        Recognises 0x00..0x0F ARP-speed repeats (re-execute the current
        row N more frames), 0xFE jump, 0xFF end-of-table (latch), and
        the ``$7F`` chord code (treated as NOP). The waveform byte is
        AND-ed with ``ptn_gate`` exactly as the player does at $1966.
        """
        ins = v.instrument
        if ins is None:
            return
        table = self._image(ins)
        if not ins.wf_table:
            # No WF table: WFARPTB reads the table terminator ($FF) right
            # away — ``cmp #$FE`` leaves carry SET, so WRPITCH adds +1 to
            # FREQ_LO — then ENDWFTB without writing a pitch. Mirror the
            # carry and the no-pitch-write hold (compose keeps the ZP).
            v.wf_pitch_carry = True
            v.wf_did_pitch_write = False
            v.skipwft_this_tick = False
            return
        # Default: this frame's WRPITCH sees carry CLEAR. Set True below
        # for past-end ($FF), explicit WF_TABLE_END, and NOP arp ($80)
        # — the SID-Wizard branches that leave carry SET via their
        # CMP #$FE / CMP #$80 comparisons. Reset BEFORE the arp_speed
        # skip check so skip frames also get a clean carry default.
        v.wf_pitch_carry = False
        v.wf_did_pitch_write = False
        v.skipwft_this_tick = False
        # Per-instrument WF-table pacing — player.asm WFARPTB
        # (line ~1933). DEC ARPSCNT; if the post-dec value is still
        # "positive" (bit 7 clear), BPL is taken and the row read is
        # skipped this frame (SKIPWFT). Else reload ARPSCNT from
        # ``arp_speed & $3F`` and continue to read the row.
        v.arp_speed_counter = (v.arp_speed_counter - 1) & 0xFF
        if v.arp_speed_counter < 0x80:
            v.skipwft_this_tick = True
            return
        v.arp_speed_counter = v.arp_speed_reload & 0x3F
        # Per-row $00..$0F ARP-speed override (the wf_speed_counter
        # path below) ALSO modulates ARPSCNT in SID-Wizard. We keep
        # wf_speed_counter as a separate gate for now since it
        # already covers the current corpus; unify once a fixture
        # uses both mechanisms together.
        if v.wf_speed_counter > 0:
            v.wf_speed_counter -= 1
            # arp_speed-stalled frames bypass WF row processing -> no
            # carry-setting CMPs, fall straight to WRPITCH with carry
            # clear (from the upstream DEC). Leave the flag False.
            return
        # Loop in case of jump commands.
        for _ in range(4):
            pos = v.wf_pos
            if pos >= len(table):
                # Past the implicit 0xFF end-marker; freeze on current
                # state. SID-Wizard's CMP #$FE with A=$FF sets carry
                # before falling through to WRPITCH; mirror that here.
                v.wf_pitch_carry = True
                return
            byte0 = table[pos]
            if byte0 == WF_TABLE_END:
                # Same as past-end above — CMP #$FE with A=$FF sets carry.
                v.wf_pitch_carry = True
                return
            if byte0 == WF_TABLE_JUMP:
                # WFAJUMP (player.asm:2449): ``BMIauto ENDWFTB`` abandons the
                # jump for a target >= $80; otherwise ``sta WFTPOS,x`` takes
                # the byte verbatim as an offset from the instrument base.
                raw = table[pos + 1] if pos + 1 < len(table) else 0
                if raw >= 0x80 or raw >= len(table):
                    return
                v.wf_pos = raw
                continue
            if byte0 < 0x10:
                # Per-row ARP-speed override — player.asm WFTdone branch:
                # ``sta ARPSCNT,x ; bcc SETJARP``. ARPSCNT is set to the
                # wave byte, the SEWFARP waveform-write is skipped, but
                # the arp byte IS still read and pitch updated this
                # frame. The ARPSCNT-DEC at the next frame's WFARPTB
                # then SKIPWFTs the row read until the counter wraps
                # past $FF, holding the just-played note for ``byte0``
                # additional frames.
                v.arp_speed_counter = byte0
            else:
                # Apply the waveform: the byte is the SID voice-control
                # value, AND-ed with the gate mask to gate it off when
                # appropriate.
                v.sid_ctrl = byte0 & v.ptn_gate
            if pos + 1 < len(table):
                arp = table[pos + 1]
                # Match SID-Wizard's player.asm NORMARP branch
                # (native/sources/include/player.asm:~1988):
                #   $00..$7E  -> relative pitch UP   (note + arp)
                #   $7F       -> jump into chord table
                #   $80       -> NOP (don't change pitch)
                #   $81..$DF  -> absolute pitch      (arp & 0x7F)
                #   $E0..$FF  -> relative pitch down (arp - 0x100)
                if arp == WF_ARP_CHORD:
                    if self._tick_chord(v):
                        # $7E chord end: player.asm advances past this
                        # row and immediately re-processes the next
                        # one in the same frame (``jmp RDWFROW``).
                        v.wf_pos += WF_ROW_STRIDE
                        continue
                    # Chord pitch was applied ($7F loop or normal
                    # pitch). SID-Wizard's PLYCHRD branch leaves
                    # WFTPOS unchanged — only the $7E end-marker
                    # advances it (via the explicit ``adc #3-1; sta
                    # WFTPOS,x`` in PLYCHRD). Return without the
                    # row-stride advance below.
                    return
                elif arp == WF_ARP_NOP:
                    # NORMARP's NOP path: ``CMP #$80; BEQ ENDWFTB``.
                    # The equality CMP sets carry before BEQ -> WRPITCH
                    # sees C=1 and adds +1 to FREQ_LO. Vibrato's mutation
                    # of FREQLO/FREQHI ZP is NOT wiped here, so the
                    # accumulated vibrato_offset must persist.
                    v.wf_pitch_carry = True
                elif arp >= WF_ARP_REL_DOWN_BASE:
                    v.wf_arp_pitch = arp - 0x100
                    v.wf_arp_absolute = False
                    # RELPTCH overwrites FREQLO/FREQHI ZP via
                    # ``adc DPITCH; tay; lda FREQTBL,y; sta FREQLO,x``
                    # (player.asm line 2035-2048). Vibrato's mutation
                    # is wiped — reset our accumulated offset to match,
                    # and flag so the in-flight vibrato step is also
                    # suppressed.
                    v.vibrato_offset = 0
                    v.wf_did_pitch_write = True
                    # RELPTCH's ``clc ; adc DPITCH`` sets carry when
                    # ``arp + DPITCH > $FF``. The carry propagates
                    # intact through ``and #$7F ; tay ; lda FREQTBL ;
                    # sta FREQLO ; lda FREQTBH ; sta FREQHI ; lda
                    # FREQLO`` (none affect carry) into ``adc DETUNER``
                    # = +1 on SID FREQ_LO. For arp $E0..$FE (signed
                    # -32..-2), this happens whenever DPITCH > $100-arp.
                    dpitch = (v.note + v.octave_shift + v.transpose) & 0xFF
                    if arp + dpitch > 0xFF:
                        v.wf_pitch_carry = True
                elif arp >= 0x80:
                    v.wf_arp_pitch = arp & 0x7F
                    v.wf_arp_absolute = True
                    # ABSPTCH overwrites FREQLO/FREQHI ZP, same as RELPTCH.
                    v.vibrato_offset = 0
                    v.wf_did_pitch_write = True
                else:
                    # $00..$7E: relative pitch up.
                    v.wf_arp_pitch = arp
                    v.vibrato_offset = 0
                    v.wf_arp_absolute = False
                    v.wf_did_pitch_write = True
            if pos + 2 < len(table):
                detune = table[pos + 2]
                if detune != 0xFF:
                    v.detune = detune - 0x100 if detune >= 0x80 else detune
            v.wf_pos += WF_ROW_STRIDE
            return

    def _tick_pw_table(self, v: VoiceState) -> None:
        """Advance the pulse-width table by one step.

        ``pw_pos`` is an ABSOLUTE offset from the instrument base, matching
        SID-Wizard's PWTPOS, and is walked against
        :meth:`Instrument.memory_image`. A position the player has not reset
        can therefore land in another table's bytes, or (below
        :data:`INST_WF_TABLE_POS`) in the header — exactly as the real
        player does.
        """
        ins = v.instrument
        if ins is None:
            return
        image = self._image(ins)
        for _ in range(4):
            idx = v.pw_pos
            if idx < 0 or idx >= len(image):
                return
            byte0 = image[idx]
            if byte0 == PW_TABLE_END:
                return
            if byte0 == PW_TABLE_JUMP:
                # JUMP target is an absolute instrument offset.
                tgt = image[idx + 1] if idx + 1 < len(image) else 0
                if tgt >= len(image):
                    return
                v.pw_pos = tgt
                # Per player.asm PWTJUMP (~line 1893): if the target's
                # first byte is sweep mode (< $80), ``bpl SEPWPOS``
                # just sets PWTPOS and returns — the sweep does not
                # advance this frame. Only set-mode targets fall
                # through to SETPULW and process in the same tick.
                if image[tgt] < 0x80:
                    v.pw_sweep_count = 0
                    return
                continue
            if byte0 & 0x80:
                # Set-mode row: high byte (low 7 bits -> pw_hi), then a
                # PW-low byte at +1. Column 2 is the PW keyboard-track
                # value (stashed for compose to apply via EXPTAB).
                v.pw_hi = byte0 & 0x7F
                if idx + 1 < len(image):
                    v.pw_lo = image[idx + 1]
                if idx + 2 < len(image):
                    v.pw_kbd_track = image[idx + 2]
                v.pw_pos += PW_ROW_STRIDE
                v.pw_sweep_count = 0
                return
            # Sweep mode: byte0 is the cycle countdown, byte1 is the
            # signed PW delta per frame. SID-Wizard's player.asm
            # PWSWEEP treats (PWHIGHO, PWLOGHO) as a 16-bit register
            # pair: for a negative delta it pre-decrements PWHIGHO
            # then adds the unsigned delta to PWLOGHO with carry; for
            # a positive delta it adds delta to PWLOGHO with carry
            # into PWHIGHO. We mirror that exactly so the 4-bit
            # SID-visible PW_HI nibble accumulates the same way under
            # multi-row sweeps as on the real player.
            if v.pw_sweep_count >= byte0:
                # Sweep complete -> PWADVAN. Player.asm reads col 2
                # (PKBDTRK) right before storing the new PWTPOS.
                if idx + 2 < len(image):
                    v.pw_kbd_track = image[idx + 2]
                v.pw_pos += PW_ROW_STRIDE
                v.pw_sweep_count = 0
                return
            v.pw_sweep_count += 1
            delta = image[idx + 1] if idx + 1 < len(image) else 0
            if delta & 0x80:
                # Negative delta: pre-decrement PWHIGHO.
                v.pw_hi = (v.pw_hi - 1) & 0xFF
            new_lo = v.pw_lo + delta
            v.pw_lo = new_lo & 0xFF
            if new_lo > 0xFF:
                # Add carry into PWHIGHO.
                v.pw_hi = (v.pw_hi + 1) & 0xFF
            return

    def _tick_filt_table(self, v: VoiceState) -> None:
        """Advance the filter table by one step.

        Per the SWM spec and player.asm SETFILT (~line 1812):

        * 3-byte rows: ``(action, cutoff_hi_or_delta, kbtrack)``.
        * Set-mode (``$80..$FD``): ``byte0 & $70`` = filter mode bits
          (LP/BP/HP — goes to ``$D418`` bits 4-6); ``(byte0 & $0F) << 4``
          = resonance nibble (goes to ``$D417`` high nibble);
          ``cutoff_hi`` -> ``$D416``; ``cutoff_lo`` reset to 0.
        * Sweep (``$01..$7F``): byte0 is the cycle count; the row's
          ``cutoff_delta`` byte (column 2) is the signed per-cycle
          addend to ``(cutoff_hi << 8 | cutoff_lo)``.
        * ``$FE`` jump; ``$FF`` end / freeze.

        Routing (``$D417`` low nibble) is owned by :func:`_emit_writes`,
        which ORs together each voice's filter-active bit
        (set here whenever the voice's instrument has a non-empty
        filter table).

        Filter walking is SHARED across voices: SID-Wizard's FilterProgram
        gates on ``cpx FLTCTRL+1`` (player.asm FLTCTRL) and runs only for
        the voice currently marked as controller. We mirror that here.
        """
        if self.filter_controller_voice is not v:
            return
        ins = v.instrument
        if ins is None:
            return
        table = self._image(ins)
        if not ins.filter_table:
            return
        # NOTE: voice_in_filter (= FSWITCH bit) is NOT updated here — per
        # player.asm, FILTPRG only walks the table and writes
        # cutoff/resonance; FSWITCH is only set at STRTSND's SETFLTP. See
        # _start_note for the assignment.
        for _ in range(4):
            pos = v.filt_pos
            if pos >= len(table):
                return
            byte0 = table[pos]
            if byte0 == FILT_TABLE_END:
                return
            if byte0 == FILT_TABLE_JUMP:
                target = table[pos + 1] if pos + 1 < len(table) else 0
                if target >= len(table):
                    return
                v.filt_pos = target
                # Per player.asm FLTJUMP (~line 1802): if target's
                # first byte is sweep mode (< $80), ``bpl SETFPOS``
                # just initialises filt_pos for next frame and exits.
                # Only set-mode targets fall through to SETFILT.
                target_byte = table[target] if target < len(table) else 0xFF
                if target_byte < 0x80:
                    self.filter_sweep_count = 0
                    return
                continue
            if byte0 & 0x80:
                # Set-mode. Extract filter band switches + resonance and
                # commit cutoff_hi; cutoff_lo resets to 0.
                cutoff_hi = table[pos + 1] if pos + 1 < len(table) else 0
                v.filt_hi = cutoff_hi
                v.filt_lo = 0
                self.filter_cutoff_hi = cutoff_hi
                self.filter_cutoff_lo = 0
                # $D417 high nibble = resonance; low nibble (routing) is
                # composed per-frame in _emit_writes.
                self.filter_resonance = (byte0 & 0x0F) << 4
                # $D418 high nibble = filter band/mode (LP/BP/HP).
                self.filter_mode_vol = (self.filter_mode_vol & 0x0F) | (byte0 & 0x70)
                v.filt_pos += FILT_ROW_STRIDE
                self.filter_sweep_count = 0
                return
            # Sweep mode. byte0 = cycle count, byte1 = signed delta per
            # cycle. Per player.asm FILTPRG with FINEFILTSWEEP_ON, the
            # cutoff is an 11-bit value split across CTFL_GHO (low 3
            # bits) and CTFH_GHO (high 8 bits). The 8-bit signed delta
            # is added to that 11-bit total; carry between the two
            # halves is handled implicitly by the bit-split arithmetic
            # (low 3 bits of delta go to CTFL with overflow into CTFH,
            # high 5 bits via ``LSR LSR LSR`` add to CTFH).
            if self.filter_sweep_count >= byte0:
                v.filt_pos += FILT_ROW_STRIDE
                self.filter_sweep_count = 0
                return
            self.filter_sweep_count += 1
            delta = table[pos + 1] if pos + 1 < len(table) else 0
            if delta >= 0x80:
                delta -= 0x100
            total11 = (v.filt_hi << 3) | (v.filt_lo & 7)
            total11 = (total11 + delta) & 0x7FF
            v.filt_lo = total11 & 7
            v.filt_hi = (total11 >> 3) & 0xFF
            self.filter_cutoff_hi = v.filt_hi
            self.filter_cutoff_lo = v.filt_lo
            return

    # ---- SID register assembly ---------------------------------------

    def _compose_sid_writes(self, v: VoiceState) -> None:
        """Combine the voice's accumulated state into SID-register
        operand bytes (``freq_lo/hi``, ``pw_lo/hi``, ``ad/sr``, ``ctrl``)
        for the upcoming emit."""
        ins = v.instrument
        if ins is None:
            # No instrument loaded yet (voice was woken up by a note-FX
            # like gate-off / sync before any real note triggered).
            # Reference captures show AD/SR remain at their post-reset
            # zeros; the SID-Wizard player simply doesn't touch them
            # until an instrument is selected.
            v.sid_ad = 0x00
            v.sid_sr = 0x00
            return
        v.in_hr_this_frame = v.hr_timer > 0
        if v.hr_timer > 0:
            # During the HR test-bit frame the player writes the FULL
            # post-HR ADSR (AD + SR with small-fx overrides) along with
            # CTRL = first_waveform AND ptn_gate. The HR-SR value lives
            # in the AD/SR slots already, having been written by the
            # pre-HR phase ``PRE_HR_LEAD_FRAMES`` frames earlier — see
            # ``_maybe_emit_pre_hr``. So this frame is the "release the
            # test bit and load the real note's release" transition.
            #
            # Freq and PW are left at whatever the SID was holding; the
            # register is written every frame and the MemPlayer diff /
            # forward-fill handles repeats, so those regs effectively
            # retain their pre-HR values.
            attack = v.attack_override if v.attack_override is not None else ins.attack
            decay = v.decay_override if v.decay_override is not None else ins.decay
            sustain = v.sustain_override if v.sustain_override is not None else ins.sustain
            release = v.release_override if v.release_override is not None else ins.release
            v.sid_ad = ((attack & 0xF) << 4) | (decay & 0xF)
            v.sid_sr = ((sustain & 0xF) << 4) | (release & 0xF)
            v.adsr_emit_required = True
            # STRTSND with FRAME1SWITCH_ON gates the WFGHOST + FREQ_HI
            # writes on the instrument's bit-3 (test-bit HR). For
            # instruments with bit 3 CLEAR (e.g. euphoria CUTEMEL2
            # control=$12), STRTSND skips both writes and CTRL/FREQ_HI
            # retain their pre-HR values — player.asm line 1271 has
            # ``and #8 ; beq SETWFTP`` to bypass the writes.
            if not self.features.frame1switch:
                # Flag off: the ``and #8`` test is compiled out and STRTSND unconditionally does ``lda #$09 ; sta WFGHOST,x``.
                v.sid_ctrl = 0x09
            elif ins.control & 0x08:
                v.sid_ctrl = ins.first_waveform & v.ptn_gate
                dpitch = max(
                    0,
                    min(len(NOTE_FREQ_HI) - 1, v.note + v.octave_shift + v.transpose),
                )
                v.sid_freq_hi = NOTE_FREQ_HI[dpitch]
            return

        # Post-HR: effective pitch composition. For ABSOLUTE arp
        # ($81..$DF in the WF table) the player uses the arp byte
        # directly without further transposition (player.asm ABSPTCH at
        # line 2037 just AND #$7F, no DPITCH/transpose addition). For
        # relative arp, transpose and octave_shift fold in via DPITCH.
        if not ins.wf_table:
            # Instrument with NO WF table: the asm's WFARPTB reads the
            # table terminator immediately (ENDWFTB) and never writes
            # FREQLO/FREQHI ZP, so the freq HOLDS the previous note's
            # value rather than snapping to this (often test-bit-muted)
            # note's pitch. Leave zp_freq_lo/hi untouched; WRPITCH below
            # still re-applies detune/carry to the held value.
            pass
        else:
            if v.wf_arp_absolute:
                note = v.wf_arp_pitch & 0x7F
            else:
                note = v.note + v.wf_arp_pitch + v.transpose + v.octave_shift
            note = max(0, min(len(NOTE_FREQ_LO) - 1, note))
            # ZP FREQLO/FREQHI: pre-detune, pre-carry. Vibrato_offset folds
            # in here because SID-Wizard's NORMVIB ADDFREQ/SUBFREQ writes
            # the result back to ZP FREQLO/FREQHI before WRPITCH runs.
            zp_word = (NOTE_FREQ_HI[note] << 8) | NOTE_FREQ_LO[note]
            zp_word = (zp_word + v.vibrato_offset) & 0xFFFF
            v.zp_freq_lo = zp_word & 0xFF
            v.zp_freq_hi = (zp_word >> 8) & 0xFF
        # WRPITCH (player.asm ~2051): explicit 8-bit ADC chain.
        #
        #   lda FREQLO ; adc DETUNER ; sta SID.FREQ+0
        #   lda FREQHI ; adc #0      ; sta SID.FREQ+1
        #
        # ``carry_in`` comes from whatever the LAST carry-affecting
        # 6502 instruction left set. We track the documented sources
        # via :attr:`VoiceState.wf_pitch_carry`. carry_out of the
        # FREQLO ADC propagates to the FREQHI ADC.
        carry_in = 1 if v.wf_pitch_carry else 0
        lo_sum = v.zp_freq_lo + (v.detune & 0xFF) + carry_in
        v.sid_freq_lo = lo_sum & 0xFF
        hi_sum = v.zp_freq_hi + (1 if lo_sum > 0xFF else 0)
        v.sid_freq_hi = hi_sum & 0xFF

        # Pulse width: SID's $D403 / $D40A / $D411 use bits 0-3 as the
        # high nibble of the 12-bit PW value; bits 4-7 are unused on
        # the chip. But SID-Wizard's WRPULS path writes the full
        # PWHIGHO byte (already AND-masked with $7F at SETPULW), so
        # bits 4-6 of the SID register MAY be non-zero — mirror that.
        pw_hi_sid = v.pw_hi & 0x7F
        if v.pw_kbd_track != 0 and self.features.pwkeybtrack:
            # PW keyboard tracking (player.asm WRPULS line 1914-1924):
            #   clc ; lda PKBDTRK ; beq combiKT
            #   adc DPITCH ; tay
            #   lda EXPTABH,y ; sbc EXPTABH-1,y
            # combiKT: adc PWHIGHO ; sta SIDG.PLSW+1
            # EXPTAB is an 11-byte zero prefix followed by NOTE_FREQ_HI.
            # The adc DPITCH starts with carry CLEAR; assuming no overflow
            # (DPITCH+PKBDTRK <= $FF) it stays CLEAR going into the sbc.
            # The sbc result + sbc's outgoing carry become the +adj that
            # goes into PWHIGHO.
            dpitch = (v.note + v.octave_shift + v.transpose) & 0x7F
            y = (dpitch + v.pw_kbd_track) & 0xFF
            e_y = EXPTABH[y] if y < len(EXPTABH) else 0
            e_ym = EXPTABH[y - 1] if 1 <= y <= len(EXPTABH) else 0
            # sbc EXPTABH-1,y with carry-in 0 (from clc + adc-no-overflow):
            # A = e_y - e_ym - 1 (mod 256), carry SET iff e_y >= e_ym + 1.
            sbc_a = (e_y - e_ym - 1) & 0xFF
            sbc_c = 1 if e_y >= e_ym + 1 else 0
            pw_hi_sid = (sbc_a + pw_hi_sid + sbc_c) & 0xFF
        v.sid_pw_hi = pw_hi_sid
        v.sid_pw_lo = v.pw_lo

        # NOTE: SID-Wizard's CNTPLY1/2 steady-state path never writes
        # SIDG.AD/SR; those only change via STRTSND (compose HR branch
        # above), HRGTOFF (_maybe_emit_pre_hr), or SMALFX2/3/5/6 (which
        # write SIDG.AD/SR directly via _apply_small_fx). So we leave
        # v.sid_ad / v.sid_sr alone here.
        # Note: SID-Wizard's WF arp directly stores the waveform byte
        # (including bits 1=sync and 2=ringmod) into WFGHOST. We must
        # NOT mask those off in compose, or we wipe out per-waveform
        # sync/ring settings (e.g. HEAVYBDR's WF row 0 = $47). Sync/ring
        # note-FX handlers operate directly on ``v.sid_ctrl`` instead.

    # ---- frame emit ---------------------------------------------------

    def _write_regs(self) -> None:
        """Write this frame's SID registers into the inherited memory image.

        A register a voice does not write this frame is left at its
        previous-frame value -- SID-Wizard's replay leaves the chip register
        holding its last value, so not overwriting it is the forward-fill the
        inherited :meth:`snapshot` / :meth:`play_frame` read back.
        """
        for v_idx, v in enumerate(self.voices):
            # Skip voices whose pattern hasn't reached any non-NOP row
            # yet — rain8580 v0/v1 have empty first rows that produce no
            # $D40x writes on frame 0. Bronkosaurus v2's first row is a
            # $7E gate-off note-FX, which counts as "triggered" via _apply_row.
            if not v.triggered:
                continue
            base = SID_BASE + SID_VOICE_OFFSET[v_idx]
            # When the voice has no instrument loaded, SID-Wizard's
            # TICK_2 / CNTPLAY takes the ``ldy CURINS,x; bne STRTINS;
            # jmp LEGATOO`` branch and LEGATOO ends at WRWFGHO — which
            # writes ONLY the WAVEFORM/CTRL register. FREQ_LO/HI, PW,
            # AD, SR are left at whatever the SID was holding.
            if v.instrument is None:
                self._wr(base + 4, v.sid_ctrl)
                continue
            # TICK_2 of a legato / portamento / NPORTAM row: SID-Wizard's
            # LEGATOO -> WRWFGHO writes ONLY $D404 (CTRL). FREQ/PW/AD/SR
            # are not touched; the chip retains its previous-frame values.
            if v.in_tick2_legato_this_frame:
                self._wr(base + 4, v.sid_ctrl)
                continue
            # During the HR test-bit frame SID-Wizard's STRTSND only
            # updates AD/SR/CTRL/FREQ_HI ghosts — PWLOGHO/PWHIGHO and
            # FREQ_LO are left unchanged, so SIDG.PLSW retains the
            # previous instrument's PW (or pre-song state for the
            # first trigger).
            if v.in_hr_this_frame:
                self._wr(base + 1, v.sid_freq_hi)
                self._wr(base + 5, v.sid_ad)
                self._wr(base + 6, v.sid_sr)
                self._wr(base + 4, v.sid_ctrl)
                continue
            self._wr(base + 0, v.sid_freq_lo)
            self._wr(base + 1, v.sid_freq_hi)
            self._wr(base + 2, v.sid_pw_lo)
            self._wr(base + 3, v.sid_pw_hi)
            # SID-Wizard's CNTPLY2 path (WRPULS + WRWFGHO) writes FREQ
            # + PW + CTRL but NOT AD/SR. Only write AD/SR when a code
            # path in this tick actually wrote them (STRTSND, HRGTOFF,
            # SMALFX2/3/5/6 — tracked by ``adsr_emit_required``).
            if v.adsr_emit_required:
                self._wr(base + 5, v.sid_ad)
                self._wr(base + 6, v.sid_sr)
            self._wr(base + 4, v.sid_ctrl)
        # $D417 = RESONIB (high nibble) | FSWITCH (low nibble), player.asm:1085-1087.
        res_routing = (self.filter_resonance & 0xF0) | (self.filter_switch & 0x0F)
        self._wr(SID_REG_BASE + 0x15, self.filter_cutoff_lo & 0xFF)
        # $D416 = CTFHGHO + FLSHIFT, an 8-bit adc (player.asm:1105-1110).
        self._wr(SID_REG_BASE + 0x16, (self.filter_cutoff_hi + self.filter_shift) & 0xFF)
        self._wr(SID_REG_BASE + 0x17, res_routing & 0xFF)
        self._wr(SID_REG_BASE + 0x18, self.filter_mode_vol & 0xFF)

    def snapshot(self) -> List[int]:
        """The SID register file, pulse-width-high nibble-masked.

        The SID ignores the upper nibble of the pulse-width-high registers
        (:data:`~pysidtracker.registers.PW_HI_REGS`); masking them makes the
        per-frame grid match the sidtrace oracle's
        :func:`~pysidtracker.oracle.grid_from_writes` convention (as
        :meth:`pysidtracker.oracle.EmuPlayer.snapshot` does).
        """
        base = self.SID_BASE
        return [
            (self._mem[base + i] & 0x0F) if i in PW_HI_REGS else self._mem[base + i]
            for i in range(self.REG_COUNT)
        ]
