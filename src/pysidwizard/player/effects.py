"""SWMPlayer mixin for player.asm pattern row application and INSPTFX."""

# pylint: disable=no-member,attribute-defined-outside-init,access-member-before-definition,too-few-public-methods,too-many-instance-attributes

from __future__ import annotations

import warnings
from typing import Optional

from ..constants import (
    INST_WF_TABLE_POS,
    PORTAMENTO_FX,
    RING_OFF_FX,
    RING_ON_FX,
    SYNC_OFF_FX,
    SYNC_ON_FX,
)
from ..errors import SWMUnsupportedEffectWarning
from ..model import (
    Instrument,
)
from .state import VoiceState
from .tables import (
    GATE_OFF_FX,
    GATE_ON_FX,
    NOTE_FX_MIN,
)


class Effects:
    """Row application and INSPTFX: note, instrument, small-FX and big-FX columns."""

    def _resolve_instrument(self, ref: int) -> Optional[Instrument]:
        """Map a 1..0x3E reference to an instrument; past the count -> the image's slot, else the empty one."""
        if 1 <= ref <= len(self.swm.instruments):
            return self.swm.instruments[ref - 1]
        if 1 <= ref <= 0x3E:
            return self.image.spare_instruments.get(ref, self._empty)
        return None

    def _apply_row(self, v: VoiceState, row) -> None:
        """Apply one Row's columns to the voice.

        Mirrors TICK_2: the note column (pitch and note-FX) applies only with a
        selected instrument — ``ldy CURINS,x; bne STRTINS`` else LEGATOO
        (player.asm:1326) — so NGATEON/NGATEOF leave PTNGATE at INITER's 0.
        """
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
            ins = self._resolve_instrument(instrument)
            if ins is not None:
                v.instrument = ins
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
        if note is not None and note != 0 and v.instrument is not None:
            if note < NOTE_FX_MIN:
                self._clegato(v, note)
                if fx == 0x03:
                    # Portamento big-fx: skip HR / table reset and
                    # set up the slide. ``fx_value`` is the portamento
                    # speed (passed as A to SETFMOD); FREQLO/FREQHI keep the
                    # old note, which PORTAME slides to the new DPITCH.
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
                    v.slide_vib = 0x83
                elif instrument == 0x3F:
                    # Legato (CURIFX=$3F): SID-Wizard's CHILEGA branch
                    # at player.asm line 1373-1379 sets up a maximal-speed
                    # tone-portamento:
                    #   SETLEGA lda #$7F ; sta FREQMODH,x
                    #   SETPRTA lda #$83 ; sta SLIDEVIB,x
                    # Then LEGATOO runs INSPTFX + WRWFGHO. The next
                    # PORTAME tick slides FREQ instantly to the new
                    # target (FREQMODH=$7F = $7F00 = huge step -> snaps).
                    v.slide_vib = 0x83
                    v.vibrato_freqmod_hi = 0x7F
                else:
                    # New pitched note -> start sound.
                    self._start_note(v, instrument_selected)
                    started_note = True
            elif note == GATE_OFF_FX:
                self._ngateof(v)
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
                lo, hi = self._freqmod(110, v.dpitch)
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
                    lo, hi = self._freqmod(fx_value, v.dpitch)
                    v.vibrato_freqmod_lo = lo
                    v.vibrato_freqmod_hi = hi
            elif fx == 0x02 and fx_value is not None:
                # BIGFX02 (player.asm line 2838): pitch slide DOWN.
                v.slide_vib = 0x82
                if fx_value == 0:
                    v.vibrato_freqmod_lo = 0
                    v.vibrato_freqmod_hi = 0
                else:
                    lo, hi = self._freqmod(fx_value, v.dpitch)
                    v.vibrato_freqmod_lo = lo
                    v.vibrato_freqmod_hi = hi
            elif fx == 0x03 and fx_value is not None and not portamento_applied:
                # BIGFX03 (player.asm:3505) with no new note in the row: SETSLID arms
                # SLIDEVIB=$83 and SETFMOD recomputes the speed against the held DPITCH,
                # so PORTAME keeps sliding toward the note already playing.
                v.slide_vib = 0x83
                v.vibrato_freqmod_lo, v.vibrato_freqmod_hi = self._freqmod(fx_value, v.dpitch)
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
            elif fx == 0x08 and fx_value is not None:
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
                    self.filt_pos = self._filt_table_start(ins) + target
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
                self._set_tempo(self.voices, fx_value >> 4, fx_value & 0x0F, funk=True)
            elif fx == 0x13 and fx_value is not None:
                # BIGFX13 (player.asm:3658): track single tempo, this voice only.
                self._set_tempo([v], fx_value & 0x7F, fx_value & 0x7F)
            elif fx == 0x14 and fx_value is not None:
                # BIGFX14 (player.asm:3669): track funktempo, this voice only.
                self._set_tempo([v], fx_value >> 4, fx_value & 0x0F, funk=True)
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
            if ins is None and not keep:
                # SETINBH / SETINBL (player.asm:3214-3223) read byte 3 / 4 through PLAYERZP.
                y = 3 if high < 0x40 else 4
                other = self._zp_byte(v, y) & (0x0F if high in (0x20, 0x50) else 0xF0)
                merged = other | (low << 4 if high in (0x20, 0x50) else low)
                if y == 3:
                    v.sid_ad = merged
                else:
                    v.sid_sr = merged
                v.adsr_emit_required = True
            elif ins is not None:
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
            # SMALFXE (player.asm:2748) merges the nibble into WFGHOST's low nibble; from 1.5 its
            # ``jmp WRITEWF`` is commented out, leaving a no-op.
            if self.image.quirks.wfctrl_fx_stores and self.features.wfctrl_smallfx:
                v.sid_ctrl = (v.sid_ctrl & 0xF0) | low
        elif high == 0x70:
            self._select_chord(v, low)
        elif high == 0xC0:
            # SMALFXC (player.asm:3391): ARPSPED := nibble, ARPSCNT := $FF.
            v.arp_speed_reload = low
            v.arp_speed_counter = 0xFF
        elif high == 0x80:
            # SMALFX8 = VIBAMFX (player.asm:3338): the nibble overrides PLAYERZP's vibrato byte's amplitude, then BIGFX08.
            self._apply_vibrato_byte(v, (low << 4) | (self._zp_byte(v, 5) & 0x0F))
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

    def _select_chord(self, v: VoiceState, chord: int) -> None:
        """SMALFX7 (player.asm:3325): CURCHORD := chord, CHORDPOS := CHDPTRLO[chord]."""
        if 1 <= chord <= len(self._chord_starts):
            v.current_chord = chord
            v.chord_pos = self._chord_starts[chord - 1]

    def _set_cutoff_hi(self, value: int) -> None:
        """Write CTFH_GHO (player.asm:2179), the single global cutoff-high ghost."""
        self.filter_cutoff_hi = value & 0xFF

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
