"""SWMPlayer mixin for player.asm CLEGATO / VIBSLIDE / PORTAME / SETFMOD."""

# pylint: disable=no-member,attribute-defined-outside-init,access-member-before-definition,too-few-public-methods,too-many-instance-attributes

from __future__ import annotations

from .state import VoiceState
from .tables import (
    EXP_MAX_INDEX,
    EXP_THRESHOLD,
    NONCALC_MAX_INDEX,
    NONCALC_THRESHOLD,
)


class Pitch:
    """CLEGATO / VIBSLIDE / PORTAME / SETFMOD: legato, vibrato, portamento, freq modulation."""

    def _clegato(self, v: VoiceState, note: int) -> None:
        """CLEGATO's DPITCH: the note plus the PLAYERZP instrument's octave shift and TRANSP."""
        v.note = note
        octave = v.instrument.octave_shift if self.features.octaveshift else 0
        transpose = v.transpose_pending if self.features.transposesupp else 0
        v.dpitch = (note + octave + transpose) & 0xFF

    def _tick_vibrato(self, v: VoiceState) -> None:
        """Advance vibrato OR tone-portamento by one frame.

        Handles these slide_vib modes:
        * NORMVIB (any type $01..$7F, and $00 without VIBRATOTYPES):
          delay-then-triangle vibrato as in player.asm NORMVIB + DOVIBRA,
          adding / subtracting FREQMOD to FREQLO/FREQHI.
        * Slides ($81 / $82) add / subtract FREQMOD.
        * Tone portamento ($83, set by big-fx 03): PORTAME steps FREQLO/FREQHI
          toward FREQTBL[DPITCH] (:meth:`_tick_portamento`).

        Skipped when there is no instrument loaded or when the
        slide_vib type isn't one of the above.
        """
        if v.instrument is None:
            return
        if v.slide_vib == 0x83 or (v.slide_vib == 0xFF and not self.features.portavibra):
            # SLIDES: a note-FX portamento's $FF falls through to PORTAME
            # unless PORTAVIBRA_ON sends it to TARGETN (player.asm:2096-2098).
            self._tick_portamento(v)
            return
        if v.slide_vib == 0xFF:
            self._targetn(v)
            return
        mod = (v.vibrato_freqmod_hi << 8) | v.vibrato_freqmod_lo
        if v.slide_vib == 0x81:
            # SLIDES with SLIDEVIB=$81: ADDFREQ, FREQLO/FREQHI += FREQMOD (player.asm:1644).
            self._add_freq(v, mod)
            return
        if v.slide_vib == 0x82:
            # SLIDES with SLIDEVIB=$82: SUBFREQ, FREQLO/FREQHI -= FREQMOD (player.asm:1642).
            self._add_freq(v, -mod)
            return
        if v.slide_vib == 0x00 and self.features.vibratotypes:
            # INCVIBR (player.asm line 1688): the FREQMOD amplitude grows
            # each frame by VIDELCNT (the vibrato-delay byte, reused as
            # the increase ratio — never decremented here), then falls
            # straight through to the shared DOVIBRA triangle. No delay
            # phase. With FREQMOD seeded to 0 in _setup_vibrato this
            # ramps the vibrato depth up from silence.
            mod = ((v.vibrato_freqmod_hi << 8) | v.vibrato_freqmod_lo) + (
                v.vibrato_delay_counter & 0xFF
            )
            v.vibrato_freqmod_lo = mod & 0xFF
            v.vibrato_freqmod_hi = (mod >> 8) & 0xFF
        elif v.slide_vib < 0x80:
            # NORMVIB: ``ldy SLIDEVIB,x ; bpl NORMVIB`` (player.asm:2086-2089).
            if v.vibrato_delay_counter >= 0:
                v.vibrato_delay_counter -= 1
                return
        else:
            return
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
        self._add_freq(v, mod if asl_result < v.vibrato_period else -mod)

    @staticmethod
    def _add_freq(v: VoiceState, delta: int) -> None:
        """ADDFREQ / SUBFREQ: FREQLO/FREQHI += ``delta`` (16-bit wrap)."""
        word = (((v.zp_freq_hi << 8) | v.zp_freq_lo) + delta) & 0xFFFF
        v.zp_freq_lo, v.zp_freq_hi = word & 0xFF, word >> 8

    def _targetn(self, v: VoiceState) -> None:
        """TARGETN: FREQLO/FREQHI := FREQTBL/FREQTBH at DPITCH."""
        i = v.dpitch & 0xFF
        v.zp_freq_lo, v.zp_freq_hi = self.freq_lo[i], self.freq_hi[i]

    def _tick_portamento(self, v: VoiceState) -> None:
        """PORTAME: step FREQLO/FREQHI one FREQMOD toward FREQTBL[DPITCH], or snap to it.

        SID-Wizard 1.94's default ``PORTAVIBRA_ON=0`` build does NOT
        terminate the slide when target is reached — PORTEND just
        snaps FREQ to target via TARGETN and SLIDEVIB stays $83 until
        either a CHVIBFX note-FX (CURNOT in $60..$77) calls FORCVIB or
        the next pitched note's STRTSND overwrites SLIDEVIB. With
        PORTAVIBRA_ON, PORTEND first runs FORCVIB and SETVIB1 (player.asm:2118),
        restoring the instrument's vibrato.
        """
        # ``sbc FREQLO`` / ``sbc FREQHI`` borrow the carry SLIDES leaves: ``cpy #$82``
        # sets it, PORTAVIBRA_ON's later ``cpy #$FF`` clears it. The 16-bit
        # difference's carry picks PORTAUP or PORTADN, whose own carry into
        # ADDFREQ / SUBFREQ makes the step FREQMOD + 1.
        cur = (v.zp_freq_hi << 8) | v.zp_freq_lo
        mod = (v.vibrato_freqmod_hi << 8) | v.vibrato_freqmod_lo
        borrow = 1 if self.features.portavibra else 0
        diff = self._dpitch_freq(v.dpitch) - cur - borrow
        if diff >= 0:
            if diff < mod:
                self._portend(v)
            else:
                self._add_freq(v, mod + 1)
        elif diff + mod >= 0:
            self._portend(v)
        else:
            self._add_freq(v, -mod - 1)

    def _portend(self, v: VoiceState) -> None:
        """PORTEND / TARGETN (player.asm:2117): the slide reached its target note."""
        self._targetn(v)
        if self.features.portavibra and v.instrument is not None:
            self._apply_vibrato_byte(v, v.instrument.vibrato)

    def _begin_portamento(self, v: VoiceState, fx_value: int) -> None:
        """Start tone portamento (big-fx $03) toward the current
        ``v.note`` target. Mirrors player.asm BIGFX03 -> SETSLID ->
        SETFMOD plus the SLIDEVIB=$83 marker.

        ``fx_value`` is the row's big-fx value (= portamento speed
        passed as ``A`` to SETFMOD). The FREQMOD lookup uses
        ``(fx_value >> 1) + target_pitch`` per the calculated-slide
        path in SETFMOD (LSR then ADC DPITCH).

        FREQLO/FREQHI keep the previous pitch; PORTAME slides it to the target.
        """
        target_note = v.dpitch
        v.slide_vib = 0x83
        # SETFMOD index: ``lsr A ; adc DPITCH,x`` — the lsr sets carry to
        # bit 0 of fx_value, then adc adds DPITCH plus that carry.
        # Effective index = ceil(fx_value / 2) + DPITCH, NOT floor.
        # For odd fx_value this differs from a naive ``fx_value >> 1``.
        v.vibrato_freqmod_lo, v.vibrato_freqmod_hi = self._freqmod(fx_value, target_note)

    def _setup_vibrato(self, v: VoiceState) -> None:
        """Initialise per-voice vibrato state from the instrument's
        ``vibrato`` and ``vibrato_delay`` bytes: STRTSND's SLIDEVIB, then
        SETVIB0 (player.asm:2890) and :meth:`_setvibr`.

        VIDELCNT takes the delay byte whole, so one with bit 7 set starts
        the vibrato at once. INCVIBR (SLIDEVIB=$00 with VIBRATOTYPES) reuses
        it as FREQMOD's per-frame increase.
        """
        ins = v.instrument
        v.slide_vib = ins.control & 0x30 if ins is not None else 0
        if ins is None:
            v.vibrato_delay_counter = 0
            v.vibrato_period = 0
            v.vibrato_freq_counter = 0
            v.vibrato_freqmod_lo = 0
            v.vibrato_freqmod_hi = 0
            return
        delay = ins.vibrato_delay & 0xFF
        v.vibrato_delay_counter = delay - 0x100 if delay & 0x80 else delay
        self._setvibr(v, ins.vibrato)

    def _setvibr(self, v: VoiceState, vibrato_byte: int) -> None:
        """SETVIBR (player.asm:2894-2911): VIBFREQU, VIBRACNT and FREQMOD from ``vibrato_byte``.

        VIBRACNT starts at a quarter period; with VIBRATOTYPES a SLIDEVIB of
        $20 starts it at half and $30 at zero.
        """
        freq_nibble = vibrato_byte & 0x0F
        amp_nibble = (vibrato_byte >> 4) & 0x0F
        v.vibrato_period = freq_nibble * 2
        if not self.features.vibratotypes or v.slide_vib < 0x20:
            v.vibrato_freq_counter = freq_nibble >> 1
        elif v.slide_vib == 0x30:
            v.vibrato_freq_counter = 0
        else:
            v.vibrato_freq_counter = freq_nibble
        if amp_nibble == 0:
            v.vibrato_freqmod_lo = 0
            v.vibrato_freqmod_hi = 0
            return
        v.vibrato_freqmod_lo, v.vibrato_freqmod_hi = self._freqmod(amp_nibble << 3, v.dpitch)

    def _apply_vibrato_byte(self, v: VoiceState, vibrato_byte: int) -> None:
        """BIGFX08 (player.asm:3518): FORCVIB then SETVIBR (player.asm:2894).

        FORCVIB re-reads the vibrato type from the instrument control byte;
        SETVIBR derives VIBFREQU / VIBRACNT and the FREQMOD amplitude, which
        SETFMOD leaves at zero for an amplitude nibble of 0 (``beq wrFmodL``).
        """
        v.slide_vib = self._zp_byte(v, 0) & 0x30
        self._setvibr(v, vibrato_byte)

    def _dpitch_freq(self, dpitch: int) -> int:
        """FREQTBL/FREQTBH word at DPITCH, which the asm indexes unmasked (``ldy DPITCH,x``)."""
        i = dpitch & 0xFF
        return (self.freq_hi[i] << 8) | self.freq_lo[i]

    def _lookup_freqmod(self, index: int) -> tuple[int, int]:
        """LOOKUPA / CHKTEND / calcVib (player.asm:2925-2940): FREQMOD from ``index``.

        Below :data:`EXP_THRESHOLD` the fine half is ``EXPTABH[index]`` with a zero
        high byte; at or above it the rough half is the ``FREQTBL`` / ``FREQTBH``
        pair at ``index - EXP_THRESHOLD``, with MAXSLID clamping the index one
        entry past the tables' 96 notes.
        """
        y = min(index & 0xFF, EXP_MAX_INDEX)
        if y < EXP_THRESHOLD:
            return self.exptabh[y], 0
        i = y - EXP_THRESHOLD
        return self.freq_lo[i], self.freq_hi[i]

    def _freqmod(self, raw_a: int, dpitch: int) -> tuple[int, int]:
        """SETFMOD (player.asm:2915-2941) for the current build's exponent table.

        With CALCVIBRATO_ON the index is ``ceil(A/2) + DPITCH`` into EXPTABH; with
        the flag off both steps are compiled out and EXPTBASE becomes ``FREQTBH-1``,
        so the amplitude is pitch-independent.
        """
        if not self.features.calcvibrato:
            y = min(raw_a & 0xFF, NONCALC_MAX_INDEX)
            if y < NONCALC_THRESHOLD:
                return (0 if y == 0 else self.freq_hi[y - 1]), 0
            i = y - NONCALC_THRESHOLD
            return self.freq_lo[i], self.freq_hi[i]
        if raw_a & 0xFF == 0:
            return 0, 0  # ``beq wrFmodL``
        # ``adc DPITCH,x ; tay`` (player.asm:2919-2920) keeps the full byte; LOOKUPA's own MAXSLID does the bounding.
        return self._lookup_freqmod(((raw_a + 1) >> 1) + (dpitch & 0xFF))
