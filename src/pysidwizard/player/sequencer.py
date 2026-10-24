"""SWMPlayer mixin for player.asm PLAYER / TICK_0..2 / PTN_SEQ."""

# pylint: disable=no-member,attribute-defined-outside-init,access-member-before-definition,too-few-public-methods,too-many-instance-attributes

from __future__ import annotations

from typing import List

from ..model import (
    End,
    Loop,
    MainVolume,
    PlayPattern,
    SubtuneJump,
    TempoOverride,
    Transpose,
)
from .state import VoiceState
from .tables import (
    NOTE_FX_MIN,
)


class Sequencer:
    """PLAYER / TICK_0..2 / PTN_SEQ: frame and voice ticks, orderlist, tempo, multispeed."""

    def _frame(self) -> None:
        """Advance one player tick, writing this frame's SID registers to memory.

        For ``frame_speed > 1`` tunes (e.g. euphoria's frame_speed=2),
        consecutive calls alternate between full PLAYER ticks and
        MULPLY (multispeed) ticks. MULPLY ticks only walk the WF
        table (and PW / Filter when the instrument's arp_speed has
        the bit-6 / bit-7 multispeed flags set) without advancing
        rows or running HR setup.
        """
        if self.image.play_gate:
            _, mask, play_sign = self.image.play_gate
            self._play_gate ^= mask
            if self._play_gate & 0x80 != play_sign:
                return
        is_multispeed_tick = self.swm.frame_speed > 1 and self._multispeed_phase != 0
        if self.features.retainzeropage:
            # PLAYER restores the caller's PLAYERZP on return (player.asm:1229-1235).
            self._zp = self.image.zp_init
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

    def _tick_voice(self, v: VoiceState) -> None:
        """One frame's worth of advance for ``v``: tempo / row / table."""
        # Reset the per-tick LEGATOO flag at the START of every tick so it
        # only sticks when set explicitly below in the legato branch and
        # only governs THIS tick's emit. Otherwise a PLAYER-tick legato
        # would leak the flag into a following MULPLY tick which DOES
        # emit all SID writes via SID-Wizard's MULCNTP -> WRPULS chain.
        v.in_tick2_legato_this_frame = False
        v.adsr_emit_required = False
        v.zp_dummy = False
        v.pw_written = False
        # A voice that never played a note and has no pattern is a true
        # no-op. A voice that REACHED its orderlist terminator
        # (``sequence_ended``) is NOT frozen — it keeps ticking its
        # instrument tables on the held note below (player.asm $FE ->
        # CNTPLAY); only its row/pattern advance is pinned.
        if v.pattern is None and not v.triggered:
            return
        # Decide whether this frame is a "row tick" (advance pattern row)
        # or a between-rows frame (just continue table execution).
        current_tempo = self._current_tempo(v)
        if self._tempo_match(v, current_tempo):
            current_tempo = self._current_tempo(v)
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
        if pre_incr_sc <= 1 and v.pattern is not None:
            # TICK_0 / TICK_1 point PLAYERZP at CURPTN (player.asm:1388, 1628).
            self._zp = self._pattern_image(v.pattern)
        if pre_incr_sc == min(1, self._apply_tick(current_tempo)) and self._ends_here(v):
            # PTN_SEQ (player.asm:1653): the tick after the row read looks
            # ahead for the pattern's $FF; an orderlist $FE then pins SPDCNT
            # at TICK_1, so the row read at TICK_0 never reaches TICK_2.
            v.pending_row = None
            v.pattern = None
            v.sequence_ended = True
        v.speed_counter = (v.speed_counter + 1) & 0xFF
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
            if v.instrument is not None:
                self._zp = self._image(v.instrument)
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
        if (
            v.instrument is None
            and self._dummy is not None
            and pre_incr_sc < apply_sc
            and self._pending_row_has_new_note(v)
        ):
            # HARDRST -> HRENDER loads PLAYERZP from CURINS without testing
            # it (player.asm:1594), so before any instrument is selected the
            # setup ticks of a new note walk instrument 0's tables.
            v.instrument = self._dummy
            v.zp_dummy = True
            try:
                self._tick_note_setup(v, pre_incr_sc, apply_sc)
            finally:
                v.instrument = None
            return
        self._tick_note_setup(v, pre_incr_sc, apply_sc)

    def _tick_note_setup(self, v: VoiceState, pre_incr_sc: int, apply_sc: int) -> None:
        """The table-walking remainder of a PLAYER tick for ``v``."""
        # Hard-restart phase: SID-Wizard's player holds the test bit on
        # (CTRL=$09 for the default $1A control byte), loads post-HR
        # ADSR, and DOES NOT advance the per-voice instrument tables
        # for ``HR_FRAMES`` (1) frame. The tables resume on the first
        # post-HR frame.
        if v.instrument is not None:
            # STRTINS / HRENDER / CNTPLAY load PLAYERZP from CURINS.
            self._zp = self._image(v.instrument)
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
            # HRGTOFF's staccato ``rts`` leaves PLAYERZP at the upcoming instrument.
            self._zp = self._image(self._upcoming_hr_instrument(v, v.pending_row))
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
        # vibrato step from this frame has zero net effect, which ticking
        # vibrato first reproduces.
        if pre_incr_sc < apply_sc and self._pre_hr_instrument(v, pre_incr_sc) is not None:
            # HRGTOFF (player.asm:1568) sets PTNGATE before HRENDER walks the
            # WF table, so this tick's waveform is already gated off.
            v.ptn_gate = 0xFE
        if not in_new_note_setup:
            self._tick_vibrato(v)
        self._tick_wf_table(v)
        # HARDRST's NEWNOTE path (player.asm:1526-1543) reaches HRENDER -> WFARPTB with the
        # carry of its last compare: ``cpy #$3F`` on CURIFX, or, when HARDRESTYPES' ``beq
        # prevIns`` skips that for an empty instrument column, ``cpy #$03`` on CURFX2. Nothing
        # on to SKIPWFT's ``jmp ENDWFTB`` touches it, so WRPITCH's ``adc DETUNER`` adds it.
        # NEWNOTE runs for a pitched CURNOT ($01..$5F) without big-FX portamento, NPORTAM
        # (SLIDEVIB $FF) or legato (CURIFX $3F).
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
            and prow.instrument != 0x3F
        ):
            ifx = prow.instrument or 0
            if ifx or not self.features.hardrestypes:
                v.wf_pitch_carry = ifx >= 0x3F
            else:
                v.wf_pitch_carry = (prow.fx or 0) >= 0x03
        if not in_new_note_setup:
            self._setpwid(v)
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
        v.zp_dummy = False
        v.pw_written = False
        # ARPSCNT bit-7 set = just-had-STRTSND (counter still in
        # $80..$FF). MULCNTP skips this voice until the next PLAYER
        # tick has decremented + reloaded ARPSCNT into the positive
        # range.
        if v.arp_speed_counter >= 0x80:
            return
        if v.instrument is not None:
            self._mulcntp(v)
        elif self._dummy is not None:
            # MULCNTP never tests CURINS: INITER's zeroed ARPSCNT lets it walk
            # instrument 0's tables until the first STRTSND sets ARPSCNT=$FF.
            v.instrument = self._dummy
            v.zp_dummy = True
            try:
                self._mulcntp(v)
            finally:
                v.instrument = None

    def _mulcntp(self, v: VoiceState) -> None:
        """MULCNTP's table walk for ``v`` through its PLAYERZP instrument."""
        assert v.instrument is not None
        self._zp = self._image(v.instrument)
        self._tick_wf_table(v)
        # MULTIFI's FILTPRG falls through to SETPWID (player.asm:1321-1327).
        if v.instrument.arp_speed & 0xC0:
            self._setpwid(v)
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

    def _ends_here(self, v: VoiceState) -> bool:
        """Whether ``v``'s pattern is exhausted and its orderlist reaches its
        terminator before another pattern (SEQ-FX and loops walked, nothing
        applied)."""
        if v.sequence_ended or v.pattern is None or v.pattern_row < len(v.pattern.rows):
            return False
        sequence, pos, seen = v.sequence, v.seq_pos, set()
        while pos < len(sequence) and (id(sequence), pos) not in seen:
            seen.add((id(sequence), pos))
            cmd = sequence[pos]
            pos += 1
            if isinstance(cmd, PlayPattern):
                return False
            if isinstance(cmd, End):
                return True
            if isinstance(cmd, Loop):
                pos = cmd.position
            elif isinstance(cmd, SubtuneJump):
                target = cmd.subtune * 3 + v.channel
                if target >= len(self.swm.sequences):
                    return True
                sequence, pos = self.swm.sequences[target], 0
        return True

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
                # Delayed: store in TRANSP2; CLEGATO reads it.
                v.transpose_pending = cmd.semitones
                continue
            if isinstance(cmd, TempoOverride):
                v.tempo_left = cmd.frames_per_row
                v.tempo_right = cmd.frames_per_row
                v.tempo_left_end = True
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

    def _set_tempo(
        self, voices: List[VoiceState], left: int, right: int, funk: bool = False
    ) -> None:
        """Write a tempo pair and rewind TMPPOS to its base (player.asm:3591-3625).

        A single tempo is stored ``ora #$80``; a funktempo's first slot is not.
        """
        for voice in voices:
            voice.tempo_left = left
            voice.tempo_right = right
            voice.tempo_left_end = not funk
            voice.tempo_toggle = 0

    @staticmethod
    def _current_tempo(v: VoiceState) -> int:
        tempo = v.tempo_left if v.tempo_toggle == 0 else v.tempo_right
        return tempo or 6

    @staticmethod
    def _tempo_match(v: VoiceState, tempo: int) -> bool:
        """The frame-start SPDCNT test against TEMPOTBL (player.asm:1344-1379).

        ``sec ; sbc`` of the slot byte: a zero result (a slot without bit 7
        reached exactly) restarts SPDCNT and steps TMPPOS to the next slot, a
        signed overflow (a bit-7 slot reached or passed) restarts it at
        TMPBASE. The tempo a TICK_2 fx just set is thus already in force.
        """
        end = v.tempo_toggle or v.tempo_left_end
        entry = tempo | (0x80 if end else 0)
        sc = v.speed_counter
        diff = (sc - entry) & 0xFF
        if diff and not (sc ^ entry) & (sc ^ diff) & 0x80:
            return False
        v.speed_counter = 0
        v.tempo_toggle = 0 if diff else 1
        return True

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
