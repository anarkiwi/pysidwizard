"""Dataclasses representing the components of a SID-Wizard SWM module.

The model is built around three structured types:

* :class:`Row` and :class:`Pattern` describe a pattern as an ordered list
  of rows with optional note / instrument / fx columns. The byte-level
  NOP packing is invisible to callers.

* Subclasses of :class:`SequenceCommand` describe the entries of an
  orderlist sequence (pattern references, transposes, tempo overrides,
  and the two terminators).

* :class:`Instrument` exposes the SWM instrument layout as named fields,
  including the variable-length waveform / pulse-width / filter tables.
  Pointer arithmetic and terminator bytes are emitted automatically on
  serialisation.

Round-trip on every SWM module shipped with SID-Wizard 1.94 remains
byte-exact: see ``tests/test_roundtrip.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .constants import (
    AUTHOR_LEN,
    DEFAULT_LOAD_ADDRESS,
    INST_AD_POS,
    INST_ARP_SPEED_POS,
    INST_CONTROL_POS,
    INST_DEFAULT_CHORD_POS,
    INST_FILTER_TABLE_PTR_POS,
    INST_FIRST_WAVEFORM_POS,
    INST_GATEOFF_FILT_POS,
    INST_GATEOFF_PW_POS,
    INST_GATEOFF_WF_POS,
    INST_HEADER_SIZE,
    INST_HR_AD_POS,
    INST_HR_SR_POS,
    INST_OCTAVE_POS,
    INST_PW_TABLE_PTR_POS,
    INST_SR_POS,
    INST_VIBRATO_DELAY_POS,
    INST_VIBRATO_POS,
    PACKED_MAX,
    PACKED_MIN,
    PATTERN_NEXT_COLUMN_FLAG,
    SEQUENCE_END,
    SEQUENCE_END_WITH_LOOP,
    SEQUENCE_MAINVOL_BASE,
    SEQUENCE_TEMPO_BASE,
    SEQUENCE_TEMPO_MAX,
    SEQUENCE_TRANSPOSE_BASE,
    TABLE_END,
)
from .errors import SWMFormatError

# --------------------------------------------------------------------------
# Pattern NOP-packing primitives.
# --------------------------------------------------------------------------


def pack_pattern(unpacked: bytes) -> bytes:
    """Encode runs of zeros in ``unpacked`` as SID-Wizard packed-NOP bytes.

    Mirrors the greedy left-to-right packer used by SID-Wizard's
    ``SWMconvert``: the first zero of any consecutive run is emitted
    literally; up to eight additional zeros may then be folded into a
    single byte in the range ``PACKED_MIN..PACKED_MAX``.
    """
    packed = bytearray()
    i = 0
    n = len(unpacked)
    while i < n:
        byte = unpacked[i]
        if byte != 0 or i == 0 or unpacked[i - 1] != 0:
            packed.append(byte)
            i += 1
            continue
        nop_count = PACKED_MIN - 1
        while i + 1 < n and unpacked[i + 1] == 0 and nop_count < PACKED_MAX:
            nop_count += 1
            i += 1
        if nop_count == PACKED_MIN - 1:
            packed.append(0)
        else:
            packed.append(nop_count)
        i += 1
    return bytes(packed)


def unpack_pattern(packed: bytes) -> bytes:
    """Expand SID-Wizard packed-NOP runs in ``packed`` to literal zeros.

    A byte in ``PACKED_MIN..PACKED_MAX`` is treated as a marker only when
    the immediately preceding byte already in the output stream is either
    zero or another marker — matching the contextual rule in SID-Wizard's
    decoder. Each marker expands to ``byte - PACKED_MIN + 2`` zeros.
    """
    out = bytearray()
    for byte in packed:
        is_packed = (
            PACKED_MIN <= byte <= PACKED_MAX
            and len(out) > 0
            and (out[-1] == 0 or PACKED_MIN <= out[-1] <= PACKED_MAX)
        )
        if is_packed:
            out.extend(b"\x00" * (byte - PACKED_MIN + 2))
        else:
            out.append(byte)
    return bytes(out)


# --------------------------------------------------------------------------
# Sequence (orderlist) commands.
# --------------------------------------------------------------------------


@dataclass
class SequenceCommand:
    """Abstract base for orderlist commands.

    Subclasses define :meth:`encode`. The decoder counterpart lives in
    :func:`decode_sequence`.
    """

    def encode(self) -> bytes:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass
class PlayPattern(SequenceCommand):
    """Reference to a pattern in the module's pattern list.

    ``pattern`` is the 1-based index as written on disk — i.e.
    ``PlayPattern(1)`` plays ``swm.patterns[0]``. The value ``0`` is
    legal on disk too (it denotes SID-Wizard's reserved empty pattern).
    """

    pattern: int

    def encode(self) -> bytes:
        if not (0 <= self.pattern <= 0x7F):
            raise SWMFormatError(f"pattern reference {self.pattern} out of range 0..127")
        return bytes([self.pattern])


@dataclass
class Transpose(SequenceCommand):
    """Per-channel transpose: -16..+15 semitones (encoded 0x80..0x9F)."""

    semitones: int

    def encode(self) -> bytes:
        if not (-16 <= self.semitones <= 15):
            raise SWMFormatError(f"transpose {self.semitones} out of range -16..15")
        return bytes([(SEQUENCE_TRANSPOSE_BASE + self.semitones) & 0xFF])


@dataclass
class MainVolume(SequenceCommand):
    """Set the master volume nibble of ``$D418`` (encoded 0xA0..0xAF).

    ``level`` is the low nibble 0..15. The player applies it with a
    one-frame delay (``SEQVOLU`` -> ``MAINVOL`` in player.asm ``chVolFx``).
    """

    level: int

    def encode(self) -> bytes:
        if not (0 <= self.level <= 0x0F):
            raise SWMFormatError(f"main volume {self.level} out of range 0..15")
        return bytes([SEQUENCE_MAINVOL_BASE | self.level])


@dataclass
class TempoOverride(SequenceCommand):
    """Override the current row-delay tempo (encoded 0xB0..0xEF).

    ``frames_per_row`` is the value the player will use until the next
    override, measured in PAL frames per pattern row. The player's tempo
    band is ``0xB0..0xEF``; ``0xF0..0xFD`` are no-ops (see player.asm
    ``chTmpFx``) and decode to :class:`RawSequenceByte`.
    """

    frames_per_row: int

    def encode(self) -> bytes:
        top = SEQUENCE_TEMPO_MAX - SEQUENCE_TEMPO_BASE
        if not (0 <= self.frames_per_row <= top):
            raise SWMFormatError(f"tempo override {self.frames_per_row} out of range 0..{top}")
        return bytes([SEQUENCE_TEMPO_BASE + self.frames_per_row])


@dataclass
class End(SequenceCommand):
    """Terminate the sequence without looping (0xFE)."""

    def encode(self) -> bytes:
        return bytes([SEQUENCE_END])


@dataclass
class Loop(SequenceCommand):
    """Terminate and jump back to ``position`` within the sequence.

    On disk this is the two-byte sequence ``0xFF, position``. ``position``
    is an offset (in commands' worth of bytes) measured from the start
    of the sequence.
    """

    position: int = 0

    def encode(self) -> bytes:
        if not (0 <= self.position <= 0xFF):
            raise SWMFormatError(f"loop position {self.position} out of range 0..255")
        return bytes([SEQUENCE_END_WITH_LOOP, self.position])


@dataclass
class RawSequenceByte(SequenceCommand):
    """A single sequence byte preserved verbatim.

    Used for the player's no-op ``0xF0..0xFD`` band and for any trailing
    bytes after a terminator so files round-trip exactly.
    """

    byte: int

    def encode(self) -> bytes:
        if not (0 <= self.byte <= 0xFF):
            raise SWMFormatError(f"raw sequence byte {self.byte} out of range 0..255")
        return bytes([self.byte])


def encode_sequence(commands: List[SequenceCommand]) -> bytes:
    """Encode an orderlist as raw bytes (no trailing size byte)."""
    out = bytearray()
    for cmd in commands:
        out += cmd.encode()
    return bytes(out)


def decode_sequence(data: bytes) -> List[SequenceCommand]:
    """Decode an orderlist's bytes back into a list of commands."""
    cmds: List[SequenceCommand] = []
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        i += 1
        if b < 0x80:
            cmds.append(PlayPattern(b))
        elif 0x80 <= b <= 0x9F:
            cmds.append(Transpose(b - SEQUENCE_TRANSPOSE_BASE))
        elif SEQUENCE_MAINVOL_BASE <= b <= 0xAF:
            cmds.append(MainVolume(b & 0x0F))
        elif SEQUENCE_TEMPO_BASE <= b <= SEQUENCE_TEMPO_MAX:
            cmds.append(TempoOverride(b - SEQUENCE_TEMPO_BASE))
        elif 0xF0 <= b <= 0xFD:
            cmds.append(RawSequenceByte(b))
        elif b == SEQUENCE_END:
            cmds.append(End())
        else:  # b == SEQUENCE_END_WITH_LOOP (0xFF)
            if i < n:
                cmds.append(Loop(position=data[i]))
                i += 1
            else:
                # 0xFF without a trailing position byte — preserve as-is.
                cmds.append(RawSequenceByte(b))
    return cmds


# --------------------------------------------------------------------------
# Patterns and rows.
# --------------------------------------------------------------------------


@dataclass
class Row:
    """A single pattern row.

    Setting any field to ``None`` means "no event for that column".

    Attributes:
        note: Note value in ``0..0x7F``. ``0x00..0x5F`` are pitches;
            ``0x60..0x6F`` set vibrato; ``0x78..0x7E`` are note-column
            effects (portamento, sync/ring on/off, gate on/off).
        instrument: Instrument-column byte in ``1..0x7F``. ``1..0x3E``
            selects an instrument from :attr:`SWMFile.instruments`;
            ``0x3F``-and-above values are instrument-column effects
            (legato, volume, chord-set, ...).
        fx: Effect-column byte in ``1..0xFF``. Codes ``< 0x20`` are
            "big" effects which require an ``fx_value``; codes
            ``>= 0x20`` are self-contained small effects.
        fx_value: Parameter byte for big effects.
    """

    note: Optional[int] = None
    instrument: Optional[int] = None
    fx: Optional[int] = None
    fx_value: Optional[int] = None

    def encode(self) -> bytes:
        """Serialise this row to its on-disk packed-into-stream form.

        The trailing 0xFF pattern terminator is *not* emitted: it is
        appended implicitly by the pattern's size byte.
        """
        note = self.note
        instrument = self.instrument
        fx = self.fx
        fx_value = self.fx_value
        if note is not None and not (0 <= note <= 0x7F):
            raise SWMFormatError(f"row note {note} must be in 0..127")
        if instrument is not None and not (1 <= instrument <= 0x7F):
            raise SWMFormatError(f"row instrument {instrument} must be in 1..127")
        if fx is not None and not (1 <= fx <= 0xFF):
            raise SWMFormatError(f"row fx {fx} must be in 1..255")
        has_note = note is not None and note != 0
        has_inst = instrument is not None
        has_fx = fx is not None
        is_big_fx = has_fx and fx < 0x20  # type: ignore[operator]
        if is_big_fx and fx_value is None:
            raise SWMFormatError(f"row fx {fx} is a big effect; fx_value is required")
        if not has_fx and fx_value is not None:
            raise SWMFormatError("row fx_value set without a corresponding fx")
        if not (has_note or has_inst or has_fx):
            return b"\x00"
        note_byte = (note or 0) & 0x7F
        if has_inst or has_fx:
            note_byte |= PATTERN_NEXT_COLUMN_FLAG
        out = bytearray([note_byte])
        if has_inst or has_fx:
            inst_byte = (instrument or 0) & 0x7F
            if has_fx:
                inst_byte |= PATTERN_NEXT_COLUMN_FLAG
            out.append(inst_byte)
            if has_fx:
                assert fx is not None
                out.append(fx & 0xFF)
                if is_big_fx:
                    assert fx_value is not None
                    out.append(fx_value & 0xFF)
        return bytes(out)


def _decode_rows(unpacked: bytes) -> List[Row]:
    """Parse the unpacked body of a pattern into a list of :class:`Row`."""
    rows: List[Row] = []
    i = 0
    n = len(unpacked)
    while i < n:
        note_byte = unpacked[i]
        i += 1
        note_val = note_byte & 0x7F
        row = Row(note=note_val if note_val != 0 else None)
        if note_byte & PATTERN_NEXT_COLUMN_FLAG:
            if i >= n:
                raise SWMFormatError("pattern row claims instrument column but body ended")
            inst_byte = unpacked[i]
            i += 1
            inst_val = inst_byte & 0x7F
            if inst_val != 0:
                row.instrument = inst_val
            if inst_byte & PATTERN_NEXT_COLUMN_FLAG:
                if i >= n:
                    raise SWMFormatError("pattern row claims fx column but body ended")
                row.fx = unpacked[i]
                i += 1
                if row.fx < 0x20:
                    if i >= n:
                        raise SWMFormatError("pattern row fx is big effect but value missing")
                    row.fx_value = unpacked[i]
                    i += 1
        rows.append(row)
    return rows


@dataclass
class Pattern:
    """A SWM pattern.

    Attributes:
        rows: Ordered list of :class:`Row`. The trailing 0xFF
            pattern-terminator is implicit and is *not* represented here.
        length: Musical length of the pattern in rows, as displayed by
            SID-Wizard's editor. If ``None`` at serialisation time, the
            writer uses ``len(rows)``.
        size_override: Optional override for the on-disk size byte. The
            default (``None``) emits ``len(unpacked) + 1`` which is the
            value SID-Wizard's own writer produces. Set this to ``0``
            to round-trip empty/reserved pattern slots that elide the
            row-terminator on disk.
    """

    rows: List[Row] = field(default_factory=list)
    length: Optional[int] = None
    size_override: Optional[int] = None

    @classmethod
    def decode(
        cls,
        unpacked: bytes,
        length: int,
        size_override: Optional[int] = None,
    ) -> Pattern:
        return cls(
            rows=_decode_rows(unpacked),
            length=length,
            size_override=size_override,
        )

    def encode_unpacked(self) -> bytes:
        out = bytearray()
        for row in self.rows:
            out += row.encode()
        return bytes(out)

    def effective_length(self) -> int:
        return self.length if self.length is not None else len(self.rows)


# --------------------------------------------------------------------------
# Instruments.
# --------------------------------------------------------------------------


@dataclass
class Instrument:
    """A SWM instrument.

    The first 16 bytes of an instrument's on-disk representation are
    fixed-purpose fields exposed below by name; the variable-length
    waveform / pulse-width / filter tables follow. The writer computes
    the on-disk PW and filter-table pointers automatically — callers
    only fill in the table *contents* (without the 0xFF terminators).

    All nibble-pair fields (``hr_attack``, ``hr_decay``, ``hr_sustain``,
    ``hr_release``, ``attack``, ``decay``, ``sustain``, ``release``) are
    in the SID's 0..15 range. ``octave_shift`` is a signed semitone
    offset in 12-step increments; on disk it is stored as 2's-complement.
    """

    name: bytes = b""
    control: int = 0
    hr_attack: int = 0
    hr_decay: int = 0
    hr_sustain: int = 0
    hr_release: int = 0
    attack: int = 0
    decay: int = 0
    sustain: int = 0
    release: int = 0
    vibrato: int = 0
    vibrato_delay: int = 0
    arp_speed: int = 0
    default_chord: int = 1
    octave_shift: int = 0
    gateoff_wf: int = 0
    gateoff_pw: int = 0
    gateoff_filt: int = 0
    first_waveform: int = 0
    wf_table: bytes = b""
    pw_table: bytes = b""
    filter_table: bytes = b""

    def name_str(self) -> str:
        return self.name.decode("latin-1").rstrip(" \x00")

    def table_image(self) -> bytes:
        """The instrument's contiguous after-header table memory:
        ``wf_table + 0xFF + pw_table + 0xFF + filter_table``.

        SID-Wizard's player addresses the WF / PW / filter tables by an
        absolute offset from the instrument base (WFTPOS / PWTPOS /
        FLTPOS), reset to each table's start only at STRTSND. Between
        note triggers a position can therefore point into a *different*
        table's bytes (e.g. a held PWTPOS landing in the WF region after
        a bare instrument-select). Walking this single image with offsets
        measured from :data:`INST_WF_TABLE_POS` reproduces that exactly.
        """
        return (
            self.wf_table
            + bytes([TABLE_END])
            + self.pw_table
            + bytes([TABLE_END])
            + self.filter_table
        )

    def memory_image(self) -> bytes:
        """The instrument exactly as the player addresses it: the 16-byte
        header followed by :meth:`table_image`.

        WFTPOS / PWTPOS / FLTPOS are absolute offsets from the instrument
        base, and player.asm INIPVAR (line 641) zeroes them, so offset 0 is
        the control byte rather than the first WF row: a table walk with a
        cursor the player has not reset yet reads the header as table data.
        """
        return self.encode()

    @staticmethod
    def _check_nibble(value: int, field_name: str) -> int:
        if not (0 <= value <= 0xF):
            raise SWMFormatError(f"{field_name}={value} does not fit in a 4-bit nibble")
        return value

    @staticmethod
    def _check_byte(value: int, field_name: str) -> int:
        if not (0 <= value <= 0xFF):
            raise SWMFormatError(f"{field_name}={value} does not fit in an unsigned byte")
        return value

    def encode(self) -> bytes:
        """Serialise this instrument to the bytes that precede its name.

        The trailing filter-table 0xFF terminator is *not* emitted: the
        SWM writer replaces it with the instrument's size byte.
        """
        pw_ptr = INST_HEADER_SIZE + len(self.wf_table) + 1
        filt_ptr = pw_ptr + len(self.pw_table) + 1
        for label, value in (
            ("pw_table_ptr", pw_ptr),
            ("filter_table_ptr", filt_ptr),
        ):
            if value > 0xFF:
                raise SWMFormatError(f"computed {label} {value} does not fit in a byte")
        header = bytearray(INST_HEADER_SIZE)
        header[INST_CONTROL_POS] = self._check_byte(self.control, "control")
        header[INST_HR_AD_POS] = (
            self._check_nibble(self.hr_attack, "hr_attack") << 4
        ) | self._check_nibble(self.hr_decay, "hr_decay")
        header[INST_HR_SR_POS] = (
            self._check_nibble(self.hr_sustain, "hr_sustain") << 4
        ) | self._check_nibble(self.hr_release, "hr_release")
        header[INST_AD_POS] = (self._check_nibble(self.attack, "attack") << 4) | self._check_nibble(
            self.decay, "decay"
        )
        header[INST_SR_POS] = (
            self._check_nibble(self.sustain, "sustain") << 4
        ) | self._check_nibble(self.release, "release")
        header[INST_VIBRATO_POS] = self._check_byte(self.vibrato, "vibrato")
        header[INST_VIBRATO_DELAY_POS] = self._check_byte(self.vibrato_delay, "vibrato_delay")
        header[INST_ARP_SPEED_POS] = self._check_byte(self.arp_speed, "arp_speed")
        header[INST_DEFAULT_CHORD_POS] = self._check_byte(self.default_chord, "default_chord")
        if not (-128 <= self.octave_shift <= 127):
            raise SWMFormatError(f"octave_shift={self.octave_shift} must be -128..127")
        header[INST_OCTAVE_POS] = self.octave_shift & 0xFF
        header[INST_PW_TABLE_PTR_POS] = pw_ptr
        header[INST_FILTER_TABLE_PTR_POS] = filt_ptr
        header[INST_GATEOFF_WF_POS] = self._check_byte(self.gateoff_wf, "gateoff_wf")
        header[INST_GATEOFF_PW_POS] = self._check_byte(self.gateoff_pw, "gateoff_pw")
        header[INST_GATEOFF_FILT_POS] = self._check_byte(self.gateoff_filt, "gateoff_filt")
        header[INST_FIRST_WAVEFORM_POS] = self._check_byte(self.first_waveform, "first_waveform")
        return (
            bytes(header)
            + self.wf_table
            + bytes([TABLE_END])
            + self.pw_table
            + bytes([TABLE_END])
            + self.filter_table
        )

    @classmethod
    def decode(cls, data: bytes, name: bytes) -> Instrument:
        if len(data) < INST_HEADER_SIZE:
            raise SWMFormatError(
                f"instrument data is {len(data)} bytes; need at least "
                f"{INST_HEADER_SIZE} for the fixed header"
            )
        header = data[:INST_HEADER_SIZE]
        pw_ptr = header[INST_PW_TABLE_PTR_POS]
        filt_ptr = header[INST_FILTER_TABLE_PTR_POS]
        if not (INST_HEADER_SIZE < pw_ptr <= filt_ptr <= len(data) + 1):
            raise SWMFormatError(
                f"instrument table pointers out of range: "
                f"pw={pw_ptr}, filter={filt_ptr}, body={len(data)}"
            )
        octave = header[INST_OCTAVE_POS]
        if octave >= 0x80:
            octave -= 0x100
        return cls(
            name=bytes(name),
            control=header[INST_CONTROL_POS],
            hr_attack=header[INST_HR_AD_POS] >> 4,
            hr_decay=header[INST_HR_AD_POS] & 0x0F,
            hr_sustain=header[INST_HR_SR_POS] >> 4,
            hr_release=header[INST_HR_SR_POS] & 0x0F,
            attack=header[INST_AD_POS] >> 4,
            decay=header[INST_AD_POS] & 0x0F,
            sustain=header[INST_SR_POS] >> 4,
            release=header[INST_SR_POS] & 0x0F,
            vibrato=header[INST_VIBRATO_POS],
            vibrato_delay=header[INST_VIBRATO_DELAY_POS],
            arp_speed=header[INST_ARP_SPEED_POS],
            default_chord=header[INST_DEFAULT_CHORD_POS],
            octave_shift=octave,
            gateoff_wf=header[INST_GATEOFF_WF_POS],
            gateoff_pw=header[INST_GATEOFF_PW_POS],
            gateoff_filt=header[INST_GATEOFF_FILT_POS],
            first_waveform=header[INST_FIRST_WAVEFORM_POS],
            wf_table=bytes(data[INST_HEADER_SIZE : pw_ptr - 1]),
            pw_table=bytes(data[pw_ptr : filt_ptr - 1]),
            filter_table=bytes(data[filt_ptr:]),
        )


# --------------------------------------------------------------------------
# Top-level container.
# --------------------------------------------------------------------------


@dataclass
class SWMFile:
    """A SID-Wizard SWM module.

    The amount fields stored in the on-disk header (sequence count,
    pattern count, instrument count, chord-table length, tempo-table
    length) are *derived* on write from the lengths of the corresponding
    attributes on this object — callers do not need to keep them in sync
    manually. Cross-references between sequences and patterns/instruments
    are validated at serialisation time.
    """

    frame_speed: int = 1
    highlight: int = 4
    auto_advance: int = 1  # obsolete in SWM1 but preserved for round-trip
    config_bits: int = 1  # obsolete in SWM1 but preserved for round-trip
    mute: bytes = b"\xff\xff\xff"
    default_pattern_length: int = 32
    color_theme: int = 0  # obsolete
    keyboard_type: int = 0  # obsolete
    driver_type: int = 0
    tuning_type: int = 0
    reserved: bytes = b"\x00\x00\x00"
    author: bytes = b""
    sequences: List[List[SequenceCommand]] = field(default_factory=list)
    patterns: List[Pattern] = field(default_factory=list)
    instruments: List[Instrument] = field(default_factory=list)
    chord_table: bytes = b""
    tempo_table: bytes = b""
    subtune_tempos: List[Tuple[int, int]] = field(default_factory=list)
    load_address: Optional[int] = DEFAULT_LOAD_ADDRESS

    @property
    def sequence_count(self) -> int:
        return len(self.sequences)

    @property
    def subtune_count(self) -> int:
        """Number of subtunes implied by :attr:`sequences`.

        SID-Wizard groups sequences into subtunes of three (one per SID
        channel). An empty module still has one subtune.
        """
        n = len(self.sequences)
        if n == 0:
            return 1
        return (n - 1) // 3 + 1

    def subtune(self, index: int) -> SWMFile:
        """Return a single-subtune view of this module for playback.

        SID-Wizard groups :attr:`sequences` into subtunes of three (one
        orderlist per SID channel) and keeps a funktempo pair per subtune
        in :attr:`subtune_tempos`. :class:`~pysidwizard.player.SWMPlayer`
        only ever plays the first three sequences with ``subtune_tempos[0]``,
        so to audition subtune ``index`` of a multi-subtune tune, wrap it as
        ``SWMPlayer(swm.subtune(index))``.

        The returned :class:`SWMFile` shares this module's patterns,
        instruments, and chord / tempo tables (they are global to the tune)
        but exposes only the three sequences and the single tempo pair of the
        requested subtune. ``index`` is 0-based and must be in
        ``range(self.subtune_count)``.
        """
        if not (0 <= index < self.subtune_count):
            raise IndexError(f"subtune {index} out of range 0..{self.subtune_count - 1}")
        start = index * 3
        tempos = [self.subtune_tempos[index]] if index < len(self.subtune_tempos) else []
        return SWMFile(
            frame_speed=self.frame_speed,
            highlight=self.highlight,
            auto_advance=self.auto_advance,
            config_bits=self.config_bits,
            mute=self.mute,
            default_pattern_length=self.default_pattern_length,
            color_theme=self.color_theme,
            keyboard_type=self.keyboard_type,
            driver_type=self.driver_type,
            tuning_type=self.tuning_type,
            reserved=self.reserved,
            author=self.author,
            sequences=self.sequences[start : start + 3],
            patterns=self.patterns,
            instruments=self.instruments,
            chord_table=self.chord_table,
            tempo_table=self.tempo_table,
            subtune_tempos=tempos,
            load_address=self.load_address,
        )

    def author_str(self) -> str:
        return self.author.decode("latin-1").rstrip(" \x00")

    def author_bytes_padded(self) -> bytes:
        return self.author[:AUTHOR_LEN].ljust(AUTHOR_LEN, b" ")
