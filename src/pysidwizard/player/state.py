"""Per-voice runtime state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from ..model import (
    Instrument,
    Pattern,
)


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
    # EARLY SEQ.FX"). PTN_SEQ promotes it to TRANSP each row, which only
    # CLEGATO reads, so it lands in :attr:`dpitch` with the next note.
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
    # Bit 7 on the left slot's TEMPOTBL byte: SPDCNT's match rewinds TMPPOS to it rather than stepping to the right slot.
    tempo_left_end: bool = True
    speed_counter: int = 0  # frames spent on this row

    # Note / instrument state.
    note: int = 0  # discrete pitch (0..0x5F)
    instrument_idx: int = 0
    instrument: Optional[Instrument] = None
    # DPITCH: CLEGATO's note + CURINS octave shift + TRANSP, latched at the note.
    dpitch: int = 0
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
    # FREQLO/HI ZP keep the last note's pitch: no RELPTCH/ABSPTCH since STRTSND.
    pitch_held: bool = False
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
    # SETPWID wrote SIDG.PLSW this tick (player.asm:1743-1749); HARDRST's new-note ticks skip it.
    pw_written: bool = False
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
    # True when this frame's tables were walked through instrument 0's
    # pointer (CURINS still 0), so the walk's SID writes are emitted.
    zp_dummy: bool = False

    pw_pos: int = 0
    pw_hi: int = 0
    pw_lo: int = 0
    pw_sweep_count: int = 0  # cycle countdown when sweeping
    # PKBDTRK byte: column 2 of the PW table row that's currently
    # "in effect". SID-Wizard's PWADVAN reads this byte after each
    # SET / sweep-end and stashes it for the next WRPULS, where it
    # picks an EXPTAB-based per-note adjustment to PWHIGHO.
    pw_kbd_track: int = 0
    # Carry SETPWID's table step leaves (before ENDPWTB's keyboard track).
    pw_carry: bool = False

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
