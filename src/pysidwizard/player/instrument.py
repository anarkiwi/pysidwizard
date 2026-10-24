"""SWMPlayer mixin for player.asm WFARPTB / SETJARP / PLYCHRD / SETPWID / FILTPRG / PLAYERZP."""

# pylint: disable=no-member,attribute-defined-outside-init,access-member-before-definition,too-few-public-methods,too-many-instance-attributes

from __future__ import annotations

from typing import Tuple

from ..constants import (
    INST_WF_TABLE_POS,
)
from ..model import (
    Instrument,
    Pattern,
    pack_pattern,
)
from .state import VoiceState
from .tables import (
    CHORD_END_LOOP,
    CHORD_END_RETURN,
    FILT_ROW_STRIDE,
    FILT_TABLE_END,
    FILT_TABLE_JUMP,
    PW_ROW_STRIDE,
    PW_TABLE_END,
    PW_TABLE_JUMP,
    WF_ARP_CHORD,
    WF_ARP_NOP,
    WF_ARP_REL_DOWN_BASE,
    WF_ROW_STRIDE,
    WF_TABLE_END,
    WF_TABLE_JUMP,
)


class InstrumentTables:
    """WFARPTB / SETJARP / PLYCHRD / SETPWID / FILTPRG / PLAYERZP: instrument table walks."""

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
        if not ins.wf_table and v.wf_pos >= INST_WF_TABLE_POS:
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
        # Without ARPSPEEDSUPP the dec/reload is compiled out and ``and #$3F``
        # leaves ARPSCNT non-negative, which is all MULCNTP tests.
        arp_speed = self.features.arpspeedsupp
        if arp_speed:
            v.arp_speed_counter = (v.arp_speed_counter - 1) & 0xFF
            if v.arp_speed_counter < 0x80:
                v.skipwft_this_tick = True
                return
        v.arp_speed_counter = v.arp_speed_reload & 0x3F
        # Loop in case of jump commands.
        jumped = False
        for _ in range(4):
            pos = v.wf_pos
            if jumped:
                # WFAJUMP reads the target's waveform column straight into SEWFARP: no
                # end, jump or ARP-speed test (player.asm:1965-1968).
                v.sid_ctrl = table[pos] & v.ptn_gate
            else:
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
                    if raw >= 0x80:
                        v.wf_pitch_carry = True  # RDWFROW's ``cmp #$FE`` carry reaches WRPITCH
                        return
                    if raw >= len(table):
                        return
                    v.wf_pos = raw
                    jumped = True
                    continue
                if byte0 < 0x10 and arp_speed:
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
                if arp == WF_ARP_CHORD and self.features.chordsupport:
                    # PLYCHRD: ``lda (PLAYERZP),y ; sta DETUNER,x`` with no $FF NOP test.
                    detune = table[pos + 2] if pos + 2 < len(table) else 0
                    v.detune = detune - 0x100 if detune >= 0x80 else detune
                    if self._tick_chord(v):
                        # $7E chord end: player.asm advances past this
                        # row and immediately re-processes the next
                        # one in the same frame (``jmp RDWFROW``).
                        v.wf_pos += WF_ROW_STRIDE
                        jumped = False
                        continue
                    # Chord pitch was applied ($7F loop or normal
                    # pitch). SID-Wizard's PLYCHRD branch leaves
                    # WFTPOS unchanged — only the $7E end-marker
                    # advances it (via the explicit ``adc #3-1; sta
                    # WFTPOS,x`` in PLYCHRD). Return without the
                    # row-stride advance below.
                    return
                elif arp == WF_ARP_NOP and self.features.wfarp_nop_supp:
                    # NORMARP's NOP path: ``CMP #$80; BEQ ENDWFTB``.
                    # The equality CMP sets carry before BEQ -> WRPITCH
                    # sees C=1 and adds +1 to FREQ_LO; FREQLO/FREQHI keep
                    # whatever VIBSLIDE left.
                    v.wf_pitch_carry = True
                elif 0x80 <= arp < WF_ARP_REL_DOWN_BASE:
                    v.wf_arp_pitch = arp & 0x7F
                    v.wf_arp_absolute = True
                    # ABSPTCH loads FREQLO/FREQHI from FREQTBL at the arp byte.
                    self._load_freq(v, arp)
                    v.wf_did_pitch_write = True
                else:
                    # $00..$7E relative up, $E0..$FF relative down.
                    self._relptch(v, arp)
            if pos + 2 < len(table):
                detune = table[pos + 2]
                if detune != 0xFF:
                    v.detune = detune - 0x100 if detune >= 0x80 else detune
            v.wf_pos += WF_ROW_STRIDE
            return

    def _relptch(self, v: VoiceState, arp: int) -> None:
        """RELPTCH: ``clc ; adc DPITCH,x`` then the FREQTBL load into FREQLO/FREQHI.

        The load replaces any vibrato mutation of the ZP pitch, and the add's carry
        reaches WRPITCH's ``adc DETUNER`` untouched (player.asm:2035-2050).
        """
        v.wf_arp_pitch = arp - 0x100 if arp >= 0x80 else arp
        v.wf_arp_absolute = False
        self._load_freq(v, arp + v.dpitch)
        v.wf_did_pitch_write = True
        v.wf_pitch_carry = arp + v.dpitch > 0xFF

    def _load_freq(self, v: VoiceState, index: int) -> None:
        """ABSPTCH / RELPTCH: ``and #$7F ; tay ; lda FREQTBL,y ; sta FREQLO,x`` and FREQTBH into FREQHI."""
        index &= 0x7F
        v.zp_freq_lo, v.zp_freq_hi = self.freq_lo[index], self.freq_hi[index]

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
        table = self._chord_table
        if not (1 <= v.current_chord <= len(self._chord_starts)):
            # CURCHORD is out of bounds (e.g. euphoria's TOMDRUM has
            # default_chord=$31 with only 15 chords parsed). The real
            # player reads CHDPTRLO past the end of the in-memory
            # table; we can't replicate that exact OOB read, but we
            # also know these instruments don't actually have $7F arp
            # bytes, so this branch protects against the IndexError.
            # PLYCHRD still reaches RELPTCH, with the CHORDS byte the out-of-range CHORDPOS reads.
            self._load_freq(v, v.dpitch + v.wf_arp_pitch)
            v.wf_did_pitch_write = True
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
        # DOCHORD falls through to RELPTCH.
        self._relptch(v, byte)
        v.chord_pos += 1
        return False

    def _tick_pw_table(self, v: VoiceState) -> None:
        """Advance the pulse-width table by one step (SETPWID, player.asm:2332-2376).

        ``pw_pos`` is an ABSOLUTE offset from the instrument base, matching
        SID-Wizard's PWTPOS, and is walked against
        :meth:`Instrument.memory_image`. A position the player has not reset
        can therefore land in another table's bytes, or (below
        :data:`INST_WF_TABLE_POS`) in the header -- exactly as the real
        player does. Records the carry the step leaves in ``v.pw_carry``.
        """
        ins = v.instrument
        if ins is None:
            return
        image = self._image(ins)
        idx = v.pw_pos
        if not 0 <= idx < len(image):
            return

        def col(i: int) -> int:
            return image[i] if i < len(image) else 0

        byte0 = image[idx]
        if byte0 < 0x80:
            # PWSWEEP: ``cmp PWEEPCNT,x ; beq PWADVAN`` (carry set), else one
            # signed step of the (PWHIGHO, PWLOGHO) pair.
            if v.pw_sweep_count == byte0:
                v.pw_carry = True
                self._pw_advance(v, idx, col)
                return
            v.pw_sweep_count = (v.pw_sweep_count + 1) & 0xFF
            delta = col(idx + 1)
            if delta & 0x80:
                v.pw_hi = (v.pw_hi - 1) & 0xFF
            total = v.pw_lo + delta
            v.pw_lo = total & 0xFF
            v.pw_carry = total > 0xFF
            if v.pw_carry:
                v.pw_hi = (v.pw_hi + 1) & 0xFF
            return
        if byte0 == PW_TABLE_END:
            v.pw_carry = True
            return
        if byte0 == PW_TABLE_JUMP:
            # PWTJUMP: ``cmp PWTPOS,x`` rests on a self-jump; a sweep target
            # only moves PWTPOS, any other target row is SETPULW'd at once.
            tgt = col(idx + 1)
            v.pw_carry = tgt >= idx
            if tgt == idx or tgt >= len(image):
                return
            if image[tgt] < 0x80:
                v.pw_pos = tgt
                v.pw_sweep_count = 0
                return
            idx = tgt
        else:
            v.pw_carry = False
        v.pw_hi = image[idx] & 0x7F
        v.pw_lo = col(idx + 1)
        self._pw_advance(v, idx, col)

    @staticmethod
    def _pw_advance(v: VoiceState, idx: int, col) -> None:
        """PWADVAN / SEPWPOS: latch PKBDTRK, step to the next row, reset the sweep."""
        v.pw_kbd_track = col(idx + 2)
        v.pw_pos = idx + PW_ROW_STRIDE
        v.pw_sweep_count = 0

    def _setpwid(self, v: VoiceState) -> None:
        """SETPWID ahead of WFARPTB: a SKIPWFT frame's WRPITCH adds the carry it leaves."""
        self._tick_pw_table(v)
        v.pw_written = True
        if v.skipwft_this_tick:
            v.wf_pitch_carry = self._wrpuls(v)[1] if self.features.pwkeybtrack else v.pw_carry

    def _wrpuls(self, v: VoiceState) -> Tuple[int, bool]:
        """ENDPWTB's PW-hi byte and carry (player.asm:2376-2390).

        With PWKEYBTRACK the EXPTABH step at DPITCH+PKBDTRK, its sbc borrow,
        is added to PWHIGHO; a zero PKBDTRK adds nothing with carry clear.
        """
        if not (self.features.pwkeybtrack and v.pw_kbd_track):
            return v.pw_hi & 0xFF, False
        dpitch = v.dpitch
        carry = (dpitch + v.pw_kbd_track) >> 8
        idx = (dpitch + v.pw_kbd_track) & 0xFF
        e_y = self.exptabh[idx]
        e_ym = self.exptabh[idx - 1] if idx else 0
        term = e_y - e_ym - 1 + carry
        total = (v.pw_hi & 0xFF) + (term & 0xFF) + (1 if term >= 0 else 0)
        return total & 0xFF, total > 0xFF

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
        # NOTE: voice_in_filter (= FSWITCH bit) is NOT updated here — per
        # player.asm, FILTPRG only walks the table and writes
        # cutoff/resonance; FSWITCH is only set at STRTSND's SETFLTP. See
        # _start_note for the assignment.
        for _ in range(4):
            pos = self.filt_pos
            if pos >= len(table):
                return
            byte0 = table[pos]
            if byte0 == FILT_TABLE_END:
                return
            if byte0 == FILT_TABLE_JUMP:
                target = table[pos + 1] if pos + 1 < len(table) else 0
                if target >= len(table):
                    return
                self.filt_pos = target
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
                self.filter_cutoff_hi = cutoff_hi
                self._fladvan(table, pos)
                if self.features.finefiltsweep:
                    self.filter_cutoff_lo = 0
                # $D417 high nibble = resonance; low nibble (routing) is
                # composed per-frame in _emit_writes.
                self.filter_resonance = (byte0 & 0x0F) << 4
                # $D418 high nibble = filter band/mode (LP/BP/HP).
                self.filter_mode_vol = (self.filter_mode_vol & 0x0F) | (byte0 & 0x70)
                self.filt_pos += FILT_ROW_STRIDE
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
                self._fladvan(table, pos)
                self.filt_pos += FILT_ROW_STRIDE
                self.filter_sweep_count = 0
                return
            self.filter_sweep_count += 1
            delta = table[pos + 1] if pos + 1 < len(table) else 0
            if delta >= 0x80:
                delta -= 0x100
            if self.features.finefiltsweep:
                total11 = (self.filter_cutoff_hi << 3) | (self.filter_cutoff_lo & 7)
                total11 = (total11 + delta) & 0x7FF
                self.filter_cutoff_lo = total11 & 7
                self.filter_cutoff_hi = total11 >> 3
            else:
                self.filter_cutoff_hi = (self.filter_cutoff_hi + delta) & 0xFF
            return

    def _fladvan(self, table: bytes, pos: int) -> None:
        """FLADVAN: the row's third column is the keyboard track, or from 1.4 a $8x FSWITCH."""
        if not self.features.filtkbtrack:
            return
        track = table[pos + 2] if pos + 2 < len(table) else 0
        if self.image.quirks.kbtrack_fswitch and 0x80 <= track < 0x90:
            self.filter_switch = track & 0x0F
            track = 0
        self.filter_kbd_track = track

    def _setfltp(self, v: VoiceState) -> None:
        """SETFLTP (player.asm:1929-2000): FSWITCH, FLTCTRL and FLTPOSI from the filter table's first byte.

        Any byte but $00 / $FF claims the filter (FLTCTRL := this track,
        FLTPOSI := the table start); $00 routes the voice through the filter
        without touching either; $FF (or no table) unroutes it, and STOPFLT
        points FLTPOSI at that $FF if this track holds the filter. FLTCTRL
        stays, so keyboard tracking still follows the track. FSWITCH is
        written nowhere else.
        """
        start = self._filt_table_start(v.instrument)
        ft = v.instrument.filter_table
        first = ft[0] if ft else FILT_TABLE_END
        v.voice_in_filter = first != FILT_TABLE_END
        self._set_filter_routing(v, v.voice_in_filter)
        if first not in (0x00, FILT_TABLE_END):
            self.filter_controller_voice = v
            self.filt_pos = start
        elif (
            first == FILT_TABLE_END
            and self.filter_controller_voice is v
            and self.image.quirks.filter_stop_rewinds
        ):
            self.filt_pos = start

    @staticmethod
    def _filt_table_start(ins: Instrument) -> int:
        """Absolute offset of ``ins``' filter table (its header byte $0B)."""
        return INST_WF_TABLE_POS + len(ins.wf_table) + 1 + len(ins.pw_table) + 1

    def _image(self, ins: Instrument) -> bytes:
        """The bytes PLAYERZP reads for ``ins``, cached per instrument object.

        Its :attr:`~pysidwizard.model.Instrument.ram` window when that still
        holds the instrument's encoding, so a cursor past the instrument's own
        tables reads the image's following bytes as the 6502 does.
        """
        image = self._zp_images.get(id(ins))
        if image is None:
            image = ins.memory_image()
            if ins.ram.startswith(image):
                image = ins.ram
            self._zp_images[id(ins)] = image
        return image

    def _pattern_image(self, pattern: Pattern) -> bytes:
        """The packed pattern TICK_0 / TICK_1 point PLAYERZP at (player.asm:1388, 1628).

        Its :attr:`~pysidwizard.model.Pattern.ram` window when that holds the
        same packing, so a read past the terminator sees the image's bytes.
        """
        image = self._zp_images.get(id(pattern))
        if image is None:
            image = pack_pattern(pattern.encode_unpacked()) + b"\xff"
            if pattern.ram.startswith(image):
                image = pattern.ram
            self._zp_images[id(pattern)] = image
        return image

    def _zp_image(self, v: VoiceState) -> bytes:
        """What ``lda (PLAYERZP),y`` reads during ``v``'s INSPTFX.

        STRTINS loads CURINS's instrument first; with CURINS=0 TICK_2 jumps
        straight to LEGATOO (player.asm:1752-1754), so PLAYERZP still holds
        whatever the previous load, on any track, left.
        """
        return self._image(v.instrument) if v.instrument is not None else self._zp

    def _zp_byte(self, v: VoiceState, y: int) -> int:
        image = self._zp_image(v)
        return image[y] if y < len(image) else 0
