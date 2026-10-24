"""SWMPlayer mixin for player.asm WRPITCH / ghost registers / COMMONREGS."""

# pylint: disable=no-member,attribute-defined-outside-init,access-member-before-definition,too-few-public-methods,too-many-instance-attributes

from __future__ import annotations

from pysidtracker.registers import (
    SID_BASE,
    SID_VOICE_OFFSET,
)

from .state import VoiceState
from .tables import (
    SID_REG_BASE,
    TRACK_STRIDE,
)


class Output:
    """WRPITCH / ghost registers / COMMONREGS: SID register composition and writes."""

    def _compose_sid_writes(self, v: VoiceState) -> None:
        """Combine the voice's accumulated state into SID-register
        operand bytes (``freq_lo/hi``, ``pw_lo/hi``, ``ad/sr``, ``ctrl``)
        for the upcoming emit."""
        ins = v.instrument
        if ins is None:
            # CURINS=0: LEGATOO's INSPTFX is the only AD/SR writer (player.asm:1326).
            return
        v.in_hr_this_frame = v.hr_timer > 0
        if v.hr_timer > 0:
            # During the HR test-bit frame the player writes the FULL
            # post-HR ADSR (AD + SR with small-fx overrides) along with
            # CTRL = effective_first_waveform (SWM1's $00->$09 depacker default) AND ptn_gate. The HR-SR value lives
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
            if not self.features.frame1switch or (
                ins.control & 0x08 and self.image.quirks.first_frame_fixed_waveform
            ):
                # Flag off, STRTSND does ``lda #$09 ; sta WFGHOST,x`` unconditionally; V1.0 does it on bit 3.
                v.sid_ctrl = 0x09
            elif ins.control & 0x08:
                v.sid_ctrl = ins.effective_first_waveform & v.ptn_gate
                # INSCTRL (player.asm:1839) indexes with an unmasked ``ldy DPITCH,x``;
                # pre-1.8 drivers index by the track offset X instead.
                if self.image.quirks.first_frame_freq_by_track:
                    index = v.channel * TRACK_STRIDE
                else:
                    index = v.dpitch
                v.sid_freq_hi = self.freq_hi[index]
            return

        # WRPITCH (player.asm ~2051): explicit 8-bit ADC chain.
        #
        #   lda FREQLO ; adc DETUNER ; sta SID.FREQ+0
        #   lda FREQHI ; adc #0      ; sta SID.FREQ+1
        #
        # ``carry_in`` comes from whatever the LAST carry-affecting
        # 6502 instruction left set. We track the documented sources
        # via :attr:`VoiceState.wf_pitch_carry`. carry_out of the
        # FREQLO ADC propagates to the FREQHI ADC.
        if not self.features.detunesupport:
            v.sid_freq_lo = v.zp_freq_lo
            v.sid_freq_hi = v.zp_freq_hi
        else:
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
        # ENDPWTB alone writes SIDG.PLSW, so a tick without SETPWID keeps the ghost.
        if v.pw_written:
            v.sid_pw_hi = self._wrpuls(v)[0]
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
            if not (v.triggered or v.zp_dummy):
                continue
            base = SID_BASE + SID_VOICE_OFFSET[v_idx]
            # With no instrument loaded TICK_2 takes ``ldy CURINS,x; bne STRTINS;
            # jmp LEGATOO`` (player.asm:1326), as does a legato / portamento /
            # NPORTAM row: LEGATOO's INSPTFX writes SIDG.AD/SR directly
            # (WRITEAD / WRITESR / SMALFX2..6) and WRWFGHO writes CTRL, leaving
            # FREQ and PW at what the chip holds.
            if (v.instrument is None and not v.zp_dummy) or v.in_tick2_legato_this_frame:
                if v.adsr_emit_required:
                    self._wr(base + 5, v.sid_ad)
                    self._wr(base + 6, v.sid_sr)
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
            if v.pw_written or self.features.allghostregs:
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
        # $D416 = CTFH_GHO + FLSHIFT + the controller's cutoff keyboard-track term (FILTKBTRACK).
        cutoff_hi = self.filter_cutoff_hi
        if (
            self.filter_kbd_track
            and self.features.filtkbtrack
            and self.filter_controller_voice is not None
        ):
            cv = self.filter_controller_voice
            dpitch = cv.dpitch
            idx = (dpitch + self.filter_kbd_track) & 0xFF
            cutoff_hi += self.exptabh[idx] + ((dpitch + self.filter_kbd_track) >> 8)
        self._wr(SID_REG_BASE + 0x16, (cutoff_hi + self.filter_shift + (cutoff_hi >> 8)) & 0xFF)
        self._wr(SID_REG_BASE + 0x17, res_routing & 0xFF)
        self._wr(SID_REG_BASE + 0x18, self.filter_mode_vol & 0xFF)
