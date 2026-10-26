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
module was exported for: :attr:`SWMPlayer.features`, read from a ``.sid``'s
player code (:attr:`SWMFile.player`), else derived from the SWM header's driver
byte via :func:`pysidwizard.features.features_for_driver`.
Driver types 0 (normal), 1 (Medium), 2 (Light) and 3 (Extra) are modelled.
The Extra build's DELAYSUPPORT_ON ``$1D`` / ``$1E``, and the Light build's
ARPSPEEDSUPP_ON / TEMPOPRGSUPP_ON / FILTKBTRACK_ON, are not yet gated.

Multi-SID playback (one chip is rendered, so a multi-SID module raises
:class:`~pysidwizard.errors.SWMFormatError`), SFX, slowdown and non-440 Hz
tuning tables are out of scope. Any other effect this player does not implement is
reported (never swallowed) via
:class:`~pysidwizard.errors.SWMUnsupportedEffectWarning` and tallied in
:attr:`SWMPlayer.unsupported_effects`.

A tune plays via the generic ``pysidtracker`` CLI (``pysidtracker
info|reglog|wav <tune.sid>``), which pysidwizard registers for ``.sid``
files through the ``pysidtracker.formats`` entry point.
"""

from __future__ import annotations

from typing import List, Optional

from pysidtracker.player import MemPlayer
from pysidtracker.registers import (
    PAL_CYCLES_PER_FRAME,
    SID_BASE,
    SID_VOICE_OFFSET,
)

from ..errors import SWMFormatError
from ..features import PlayerFeatures, features_for_driver
from ..model import Instrument, SWMFile
from ..variant import PlayerImage
from .effects import Effects
from .hardrestart import HardRestart
from .instrument import InstrumentTables
from .output import Output
from .pitch import Pitch
from .sequencer import Sequencer
from .state import VoiceState
from .tables import (
    CHORD_END_LOOP,
    CHORD_END_RETURN,
    DEFAULT_HR_ADSR,
    EXP_MAX_INDEX,
    EXP_THRESHOLD,
    EXPTABH,
    EXPTABH_FREQTBH_OFFSET,
    FILT_ROW_STRIDE,
    FILT_TABLE_END,
    FILT_TABLE_JUMP,
    FREQ_TABLE_SPAN,
    FREQ_TABLE_TAIL_LEN,
    FREQTBH_SLOPE,
    FREQTBL_PAST_END,
    GATE_OFF_FX,
    GATE_ON_FX,
    HR_FRAMES,
    NONCALC_MAX_INDEX,
    NONCALC_THRESHOLD,
    NOTE_FREQ_HI,
    NOTE_FREQ_LO,
    NOTE_FX_MIN,
    PRE_HR_LEAD_FRAMES,
    PW_ROW_STRIDE,
    PW_TABLE_END,
    PW_TABLE_JUMP,
    SID_REG_BASE,
    TRACK_STRIDE,
    WF_ARP_CHORD,
    WF_ARP_NOP,
    WF_ARP_REL_DOWN_BASE,
    WF_ROW_STRIDE,
    WF_TABLE_END,
    WF_TABLE_JUMP,
    _empty_instrument,
    _parse_chord_starts,
    freq_tables,
)

__all__ = [
    "CHORD_END_LOOP",
    "CHORD_END_RETURN",
    "DEFAULT_HR_ADSR",
    "EXPTABH",
    "EXPTABH_FREQTBH_OFFSET",
    "EXP_MAX_INDEX",
    "EXP_THRESHOLD",
    "FILT_ROW_STRIDE",
    "FILT_TABLE_END",
    "FILT_TABLE_JUMP",
    "FREQTBH_SLOPE",
    "FREQTBL_PAST_END",
    "FREQ_TABLE_SPAN",
    "FREQ_TABLE_TAIL_LEN",
    "GATE_OFF_FX",
    "GATE_ON_FX",
    "HR_FRAMES",
    "NONCALC_MAX_INDEX",
    "NONCALC_THRESHOLD",
    "NOTE_FREQ_HI",
    "NOTE_FREQ_LO",
    "NOTE_FX_MIN",
    "PAL_CYCLES_PER_FRAME",
    "PRE_HR_LEAD_FRAMES",
    "PW_ROW_STRIDE",
    "PW_TABLE_END",
    "PW_TABLE_JUMP",
    "SID_BASE",
    "SID_REG_BASE",
    "SID_VOICE_OFFSET",
    "SWMPlayer",
    "TRACK_STRIDE",
    "VoiceState",
    "WF_ARP_CHORD",
    "WF_ARP_NOP",
    "WF_ARP_REL_DOWN_BASE",
    "WF_ROW_STRIDE",
    "WF_TABLE_END",
    "WF_TABLE_JUMP",
    "freq_tables",
]


# pylint: disable-next=too-many-ancestors
class SWMPlayer(Sequencer, HardRestart, InstrumentTables, Pitch, Effects, Output, MemPlayer):
    """Per-frame SID-Wizard player, as a :class:`~pysidtracker.player.MemPlayer`.

    Construct from a :class:`pysidwizard.SWMFile`. The inherited
    :meth:`~pysidtracker.player.MemPlayer.play_frame` returns the
    ``(reg_offset, value)`` writes that changed this frame, and
    :meth:`~pysidtracker.player.MemPlayer.render_grid` yields the
    forward-filled per-frame register grid the shared sidtrace oracle
    compares against.

    This is a pure-Python transcription of SID-Wizard's 6502 player: the
    inherited memory image is used only as the ``$D400`` SID register file
    that :meth:`_frame` writes into and :meth:`snapshot` reads back, its
    pulse-width-high registers nibble-masked
    (:attr:`~pysidtracker.player.MemPlayer.MASK_PW_HI`) as the oracle grid is.
    """

    MASK_PW_HI = True

    def __init__(self, swm: SWMFile, features: Optional[PlayerFeatures] = None) -> None:
        if swm.sid_amount != 1:
            raise SWMFormatError(
                f"{swm.sid_amount}-SID playback is not modelled: SWMPlayer renders one chip"
            )
        self.swm = swm
        # The player variant this module was exported with: its ``.sid``'s image, else the SWM driver byte's build.
        self.image = swm.player or PlayerImage(features_for_driver(swm.driver_type))
        self.features = features or self.image.features
        if self.image.freq_lo and self.image.freq_hi:
            self.freq_lo, self.freq_hi = self.image.freq_lo, self.image.freq_hi
        else:
            self.freq_lo, self.freq_hi = freq_tables(self.features, self.image.freq_table_tail)
        # EXPTABH is 11 zero bytes then the image's own FREQTBH (player.asm:2958).
        self.exptabh = bytes(EXPTABH_FREQTBH_OFFSET) + bytes(self.freq_hi)
        self._unsupported_effects: dict = {}
        super().__init__(b"", 0)

    def _init(self, subtune: int = 0) -> None:
        swm = self.swm
        # The first play call starts from whatever the tune's init wrote (INITER's
        # SID clear loop writes $00 or, in early drivers, $08).
        self._mem[SID_BASE : SID_BASE + len(self.image.sid_init)] = self.image.sid_init

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
        # PLAYERZP images of instruments and patterns, keyed by object identity; a spare slot is its image window.
        self._zp_images: dict[int, bytes] = {
            id(ins): ins.ram for ins in self.image.spare_instruments.values()
        }

        # Per-voice state. SID-Wizard assigns the SWM sequences in the
        # order [V0, V1, V2] for subtune 0 (sequences 0, 1, 2).
        # SETSTUNE copies the subtune's tempo pair to TEMPOTBL+0/1 as stored
        # (player.asm:2657-2668): bit 7 on the left byte makes it a single
        # tempo, otherwise TMPPOS alternates the two slots (funktempo).
        tempo_left, tempo_right = swm.subtune_tempos[0] if swm.subtune_tempos else (6, 6)
        # INSPTLO/INSPTHI slot 0: the exporter links the unused instrument 0 to
        # instrument 1 (packdepack.inc:621-633) unless the image says otherwise.
        # PLAYERZP points at it whenever a path loads CURINS=0 without testing it
        # (HRENDER, MULCNTP).
        self._dummy: Optional[Instrument] = self.image.dummy_instrument or (
            swm.instruments[0] if swm.instruments else None
        )
        self._empty = _empty_instrument()
        # $D418 and its pending SEQVOLU nibble must exist before INITER's
        # orderlist walk, which can run a MainVolume seq-fx.
        self.filter_mode_vol = 0x0F  # $D418
        self.seqvolu = 0x0F  # SEQVOLU_var: pending master-volume nibble
        self.voices: List[VoiceState] = []
        for i in range(3):
            # INITER (player.asm:551) zeroes every per-track variable, so
            # SPDCNT, PTNGATE and ARPSCNT all start at 0 and the first PLAYER
            # call runs TICK_0 of row 0.
            v = VoiceState(channel=i, ptn_gate=0, arp_speed_counter=0)
            if i < len(swm.sequences):
                v.sequence = swm.sequences[i]
            v.tempo_left = tempo_left & 0x7F
            v.tempo_right = tempo_right & 0x7F
            v.tempo_left_end = bool(tempo_left & 0x80)
            # INITPTN (player.asm:788): read each orderlist up to its first
            # pattern, running any orderlist effects on the way.
            self._advance_sequence(v)
            self.voices.append(v)

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
        (
            self.filter_resonance,
            self.filter_cutoff_hi,
            self.filter_cutoff_lo,
            self.filter_switch,
        ) = self.image.filter_init
        # FSWITCH (player.asm:1085): the low nibble of $D417 -- per-voice routing bits
        # maintained by STRTSND's SETFLTP (player.asm:1999-2008), plus the filter-external
        # bit that only BIGFX1F writes; its start is the operand the tune's init leaves.
        # FLSHIFT (player.asm:1110): a signed byte BIGFX1C adds to the cutoff-high
        # ghost on every $D416 write. INITER zero-initialises it (player.asm:694).
        self.filter_shift = 0x00
        # FILTKBTRACK (player.asm SETFILT): cutoff-high keyboard-track amount, like the PW's PKBDTRK.
        self.filter_kbd_track = 0x00
        # SID-Wizard's FilterProgram is shared across voices: only the
        # one voice currently marked as filter-controller (FLTCTRL+1
        # operand byte at $1xxx) actually walks its filter table.
        # Other voices' filter_table-bearing instruments only set
        # their routing bit (voice_in_filter); the cutoff sweep is
        # owned by the controller. ``None`` = no controller selected
        # yet (matches the post-INITER default; first STRTSND of a
        # SET-row-starting instrument assigns).
        self.filter_controller_voice: Optional[VoiceState] = None
        # FLTPOSI: the one filter-table cursor, read through the controller's PLAYERZP.
        self.filt_pos = 0
        # PLAYERZP's target: the bytes ``lda (PLAYERZP),y`` reads. One pointer shared by
        # every track, so a TICK_2 with CURINS=0 (LEGATOO's INSPTFX) reads whatever the
        # last pattern or instrument load left, starting where the init left it.
        self._zp = self.image.zp_init
        # CWEPCNT (player.asm ~line 1761) is a single global byte —
        # the sweep-cycle countdown for whichever voice currently owns
        # FilterProgram. Mirror that here as a player-level (not per-
        # voice) counter.
        self.filter_sweep_count = 0

        self.frame_idx = 0
        self._play_gate = self.image.play_gate[0] if self.image.play_gate else 0
        self.finished = False

        # Multispeed state. For ``frame_speed > 1`` the exporter's CIA player
        # (exporter.asm CIAPLAY) counts calls from 0 at init: calls 1 ..
        # ``frame_speed - 1`` jump to ``mulpsub`` (MULPLY) and call
        # ``frame_speed`` resets the count and jumps to ``playsub`` (PLAYER).
        # ``_multispeed_phase`` is that count; phase 0 -> PLAYER tick.
        self._multispeed_phase = 1 % max(1, swm.frame_speed)

    @property
    def unsupported_effects(self) -> dict[tuple[str, int], int]:
        """Per-``(column, code)`` counts of the effects this render did not model."""
        return dict(self._unsupported_effects)
