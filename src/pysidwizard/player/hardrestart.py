"""SWMPlayer mixin for player.asm HARDRST / HRGTOFF / STRTSND."""

# pylint: disable=no-member,attribute-defined-outside-init,access-member-before-definition,too-few-public-methods,too-many-instance-attributes

from __future__ import annotations

from typing import Any, Optional

from ..constants import (
    INST_WF_TABLE_POS,
)
from ..model import (
    Instrument,
)
from .state import VoiceState
from .tables import (
    DEFAULT_HR_ADSR,
    HR_FRAMES,
    NOTE_FX_MIN,
)


class HardRestart:
    """HARDRST / HRGTOFF / STRTSND: hard-restart lookahead, gate-off and note start."""

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
        ref = row.instrument
        if ref is not None and 1 <= ref <= 0x3E:
            ins = self._resolve_instrument(ref) or ins
        return ins

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
        ins = self._pre_hr_instrument(v, pre_incr_sc, row)
        if ins is None:
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

    def _pre_hr_instrument(
        self, v: VoiceState, pre_incr_sc: int, row: Any = None
    ) -> Optional[Instrument]:
        """The instrument whose HRGTOFF fires on this pre-HR tick, else None."""
        ins = self._upcoming_hr_instrument(v, v.pending_row if row is None else row)
        if ins is None or not self._hr_fires_at_tick(ins, pre_incr_sc):
            return None
        return ins

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

    def _start_note(self, v: VoiceState, instrument_selected: bool = False) -> None:
        """Re-trigger the voice: reset gates / tables / hard-restart."""
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
            pw_reset_off = self.features.pwresetsw and (not instrument_selected) and (ctrl & 0x40)
            filt_reset_off = (
                self.features.filtresetsw and (not instrument_selected) and (ctrl & 0x80)
            )
            if not pw_reset_off:
                # PWTPOS resets to the PW-table START as an ABSOLUTE offset
                # from the instrument base (STRTSND ~line 1445 reads the
                # header's PW-table pointer). _tick_pw_table walks from here.
                v.pw_pos = INST_WF_TABLE_POS + len(v.instrument.wf_table) + 1
            v.wf_arp_pitch = 0
            v.wf_arp_absolute = False
            # STRTSND (player.asm line 1422) stores $FF to ARPSCNT so
            # the very first post-note WFARPTB pass falls through the
            # BPL check and reloads from ARPSPED; only MULTISPEEDSUPP_ON builds it.
            if self.features.multispeedsupp:
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
            # FILTRESETSW's ``bmi ENDFILRESET`` skips all of SETFLTP.
            if not filt_reset_off:
                self._setfltp(v)
            # Seed chord state from the instrument's default-chord field
            # (1-based; 0 means "no chord"). The WF-table arp tick will
            # consume ``chord_pos`` whenever it hits a ``$7F`` arp byte.
            chord_idx = v.instrument.default_chord if self.features.chordsupport else 0
            if 1 <= chord_idx <= len(self._chord_starts):
                v.current_chord = chord_idx
                v.chord_pos = self._chord_starts[chord_idx - 1]
            else:
                v.current_chord = 0
                v.chord_pos = 0
            self._setup_vibrato(v)

    def _ngateof(self, v: VoiceState) -> None:
        """NGATEOF (player.asm:3117-3175): gate off, or jump the tables to the gate-off pointers.

        Under GATEOFFPTR a nonzero WF pointer replaces the gate-off: WFTPOS
        takes it and PTNGATE / WFGHOST stay, so the gate follows the table.
        A zero one ANDs PTNGATE and WFGHOST with $FE in the same frame. Either
        way nonzero PW and filter pointers then set PWTPOS, and FLTPOSI when
        this track controls the filter.
        """
        ins = v.instrument
        if not self.features.gateoffptr or not ins.gateoff_wf:
            v.ptn_gate = 0xFE
            v.sid_ctrl &= 0xFE
        if not self.features.gateoffptr:
            return
        if ins.gateoff_wf:
            v.wf_pos = ins.gateoff_wf
        if ins.gateoff_pw:
            v.pw_pos = ins.gateoff_pw
        if ins.gateoff_filt and self.filter_controller_voice is v:
            self.filt_pos = ins.gateoff_filt
