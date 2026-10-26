"""SWM file-format constants.

The numeric constants below mirror those in the upstream SID-Wizard source
file ``native/sources/SWM-spec.src``. They are reproduced here so the library
can be used without the C source distribution.
"""

from dataclasses import dataclass
from enum import IntFlag

# Magic identifiers
SWM_MAGIC = b"SWM1"
SWS_MAGIC = b"SWMS"
SWMT_MAGIC = b"SWMT"
SWMQ_MAGIC = b"SWMQ"

# PC64/VICE .P00 wrapper per SWMconvert.c: "C64File"\0, 16-byte PETSCII name, 2 pad bytes, then the PRG payload.
P00_MAGIC = b"C64File\x00"
P00_NAME_OFFSET = 0x08
P00_NAME_LEN = 16
P00_HEADER_SIZE = 0x1A
SWM_CONTAINERS = ("prg", "p00")

# Header layout (offsets are relative to the SWM data start, i.e. *after* any
# 2-byte PRG load address that may prefix the file on disk).
TUNE_HEADER_SIZE = 0x40  # 64 bytes
MAGIC_POS = 0
FRAMESPEED_POS = 0x04
HIGHLIGHT_POS = 0x05
AUTO_POS = 0x06  # obsolete in SWM1
CONFIG_BITS_POS = 0x07  # obsolete in SWM1
MUTE_POS = 0x08  # 3 bytes: 0x08..0x0A
DEFAULT_PATTERN_LEN_POS = 0x0B
SEQUENCE_AMOUNT_POS = 0x0C
PATTERN_AMOUNT_POS = 0x0D
INSTRUMENT_AMOUNT_POS = 0x0E
CHORD_LENGTH_POS = 0x0F
TEMPO_LENGTH_POS = 0x10
COLOR_THEME_POS = 0x11  # obsolete in SWM1
KEYBOARD_TYPE_POS = 0x12  # obsolete in SWM1
DRIVER_TYPE_POS = 0x13
TUNING_TYPE_POS = 0x14
RESERVED_POS = 0x15  # bytes 0x15..0x17
AUTHOR_POS = 0x18
AUTHOR_LEN = TUNE_HEADER_SIZE - AUTHOR_POS  # 40 bytes

BYTE_FIELDS = frozenset({"mute", "author"})
AMOUNT_FIELDS = frozenset(
    {"sequence_amount", "pattern_amount", "instrument_amount", "chord_length", "tempo_length"}
)


@dataclass(frozen=True)
class SWMFormat:
    """One SID-Wizard native work-file layout (1–4 SIDs)."""

    sid_amount: int
    magic: bytes
    header_size: int
    fields: dict[str, tuple[int, int]]  # SWMFile attr -> (header offset, width)
    reserved: tuple[int, ...]  # header byte offsets no named field addresses
    sid_addresses: tuple[int, ...]  # SID 2..N address-id byte offsets

    @property
    def channels(self) -> int:
        """SID channels (three per chip)."""
        return 3 * self.sid_amount

    @property
    def expected_mute(self) -> int:
        """Mute-block width in bytes (three per chip)."""
        return self.fields["mute"][1]

    @property
    def expected_reserved(self) -> int:
        """Number of reserved header bytes."""
        return len(self.reserved)

    @property
    def subtune_block(self) -> int:
        """Bytes per runtime ``SUBTUNES`` block: the orderlist pointers, then the funktempo pair
        in its last two bytes, padded to 8 / 16 / 32 (settings.cfg ``PPTRLO``)."""
        return 8 << min(self.sid_amount - 1, 2)

    @property
    def restemp_shift(self) -> int:
        """``RESTEMP - TEMPOTBL`` (settings.cfg): the selected-subtune tempos the exporter
        reserves ahead of the tempo programs."""
        return 2 + 2 * self.channels

    def field(self, header: bytes, name: str, default: int = 0) -> int:
        """Scalar header field ``name``; ``default`` when the layout has no such byte."""
        pos = self.fields.get(name)
        return header[pos[0]] if pos is not None else default

    def field_bytes(self, header: bytes, name: str) -> bytes:
        """Multi-byte header field ``name``."""
        off, width = self.fields[name]
        return bytes(header[off : off + width])

    def header_meta(self, header: bytes) -> dict:
        """The :class:`~pysidwizard.model.SWMFile` metadata fields ``header`` holds."""
        scalars = (
            "frame_speed",
            "highlight",
            "auto_advance",
            "config_bits",
            "default_pattern_length",
            "color_theme",
            "keyboard_type",
            "driver_type",
            "tuning_type",
        )
        return {
            **{name: self.field(header, name) for name in scalars},
            "mute": self.field_bytes(header, "mute"),
            "author": self.field_bytes(header, "author"),
            "sid_amount": self.sid_amount,
            "sid_addresses": [header[p] for p in self.sid_addresses],
            "reserved": bytes(header[p] for p in self.reserved),
        }


SWM1_FORMAT = SWMFormat(
    sid_amount=1,
    magic=SWM_MAGIC,
    header_size=64,
    fields={
        "frame_speed": (0x04, 1),
        "highlight": (0x05, 1),
        "auto_advance": (0x06, 1),
        "config_bits": (0x07, 1),
        "mute": (0x08, 3),
        "default_pattern_length": (0x0B, 1),
        "sequence_amount": (0x0C, 1),
        "pattern_amount": (0x0D, 1),
        "instrument_amount": (0x0E, 1),
        "chord_length": (0x0F, 1),
        "tempo_length": (0x10, 1),
        "color_theme": (0x11, 1),
        "keyboard_type": (0x12, 1),
        "driver_type": (0x13, 1),
        "tuning_type": (0x14, 1),
        "author": (0x18, 40),
    },
    reserved=(0x15, 0x16, 0x17),
    sid_addresses=(),
)

SWMS_FORMAT = SWMFormat(
    sid_amount=2,
    magic=SWS_MAGIC,
    header_size=64,
    fields={
        "frame_speed": (0x04, 1),
        "mute": (0x05, 6),
        "default_pattern_length": (0x0B, 1),
        "sequence_amount": (0x0C, 1),
        "pattern_amount": (0x0D, 1),
        "instrument_amount": (0x0E, 1),
        "chord_length": (0x0F, 1),
        "tempo_length": (0x10, 1),
        "driver_type": (0x13, 1),
        "tuning_type": (0x14, 1),
        "highlight": (0x15, 1),
        "author": (0x18, 40),
    },
    reserved=(0x11, 0x12, 0x17),
    sid_addresses=(0x16,),
)

SWMT_FORMAT = SWMFormat(
    sid_amount=3,
    magic=SWMT_MAGIC,
    header_size=65,
    fields={
        "frame_speed": (0x04, 1),
        "default_pattern_length": (0x05, 1),
        "sequence_amount": (0x06, 1),
        "pattern_amount": (0x07, 1),
        "instrument_amount": (0x08, 1),
        "chord_length": (0x09, 1),
        "tempo_length": (0x0A, 1),
        "driver_type": (0x0B, 1),
        "tuning_type": (0x0C, 1),
        "highlight": (0x0D, 1),
        "mute": (0x10, 9),
        "author": (0x19, 40),
    },
    reserved=(),
    sid_addresses=(0x0E, 0x0F),
)

SWMQ_FORMAT = SWMFormat(
    sid_amount=4,
    magic=SWMQ_MAGIC,
    header_size=69,
    fields={
        "frame_speed": (0x04, 1),
        "default_pattern_length": (0x05, 1),
        "sequence_amount": (0x06, 1),
        "pattern_amount": (0x07, 1),
        "instrument_amount": (0x08, 1),
        "chord_length": (0x09, 1),
        "tempo_length": (0x0A, 1),
        "driver_type": (0x0B, 1),
        "tuning_type": (0x0C, 1),
        "highlight": (0x0D, 1),
        "mute": (0x11, 12),
        "author": (0x1D, 40),
    },
    reserved=(),
    sid_addresses=(0x0E, 0x0F, 0x10),
)

SWM_FORMATS: dict[int, SWMFormat] = {
    1: SWM1_FORMAT,
    2: SWMS_FORMAT,
    3: SWMT_FORMAT,
    4: SWMQ_FORMAT,
}
SWM_MAGIC_TO_FORMAT: dict[bytes, SWMFormat] = {
    fmt.magic: fmt for fmt in (SWM1_FORMAT, SWMS_FORMAT, SWMT_FORMAT, SWMQ_FORMAT)
}


def subtunes_for(sequence_amount: int, channels: int) -> int:
    """Subtunes implied by ``sequence_amount`` grouped into ``channels``."""
    if sequence_amount <= 0:
        return 1
    return (sequence_amount - 1) // channels + 1


# Hardware limits (used for validation, not enforced strictly in parsing).
SID_CHANNELS = 3
INSTRUMENT_NAME_LEN = 8
MAX_PATTERN_LEN = 249  # 0xF9
MAX_INSTRUMENT_SIZE = 128  # 0x80
MAX_SEQUENCE_LEN = 126  # 0x7E
MAX_PATTERN_AMOUNT = 100
MAX_INSTRUMENT_AMOUNT = 37

# Default PRG load address used by SID-Wizard's SWM export.
DEFAULT_LOAD_ADDRESS = 0x1FF8

# Pattern NOP-packing range. Each packed byte ``PACKED_MIN+k`` expands to
# ``k+2`` consecutive zero bytes when preceded by a zero or another packed
# byte in the source stream.
PACKED_MIN = 0x70
PACKED_MAX = 0x77

# Pattern note-column effect-value boundaries (informational).
NOTE_MAX = 0x5F
VIBRATO_FX = 0x60
PORTAMENTO_FX = 0x78
SYNC_ON_FX = 0x79
SYNC_OFF_FX = 0x7A
RING_ON_FX = 0x7B
RING_OFF_FX = 0x7C
GATE_ON_FX = 0x7D
GATE_OFF_FX = 0x7E

# Universal terminator used in instrument WF / PW / filter tables. On disk,
# the *final* filter-table terminator is replaced with the instrument's
# size byte; the writer handles that translation automatically.
TABLE_END = 0xFF

# Pattern row flag. The note and instrument columns of a row both use
# bit 7 to signal "the next column byte follows me". A row that consists
# of just a note (no instrument or fx) leaves the bit clear.
PATTERN_NEXT_COLUMN_FLAG = 0x80

# SWM note-column reference value. SID-Wizard's converter treats SWM
# note 49 (0x31) as the equivalent of MIDI middle C.
SWM_C5_NOTE = 49

# Sequence (orderlist) commands. Anything ``< 0x80`` in a sequence is a
# pattern reference; the values below are top-bit-set commands recognised
# by the player and by SID-Wizard's exporters.
SEQUENCE_END = 0xFE  # exit subtune without looping
SEQUENCE_END_WITH_LOOP = 0xFF  # exit, followed by a 1-byte loop position
SEQUENCE_TRANSPOSE_BASE = 0x90  # 0x90..0x9F sets a -16..+15 semitone shift
SEQUENCE_MAINVOL_BASE = 0xA0  # 0xA0..0xAF sets master volume 0..15 (delayed)
SEQUENCE_TEMPO_BASE = 0xB0  # 0xB0..0xEF overrides the subtune tempo
SEQUENCE_TEMPO_MAX = 0xEF  # player's chTmpFx band ends here; 0xF0.. are no-ops
# The byte after a 0xFF terminator: below this it is a loop position within
# the current orderlist, at or above it player.asm LOOPSEQ (line 1683) calls
# SETSEQA and restarts the channel at position 0 of another subtune.
SEQUENCE_SUBTUNE_JUMP_BASE = 0x80

# Subtune funktempo: bit 7 of a tempo byte means "use the low seven bits
# straight, do not average with the partner byte".
TEMPO_STRAIGHT_FLAG = 0x80

# Instrument layout. These offsets index the bytes *before* the 8-byte
# instrument name on disk. ``INST_WF_TABLE_POS`` doubles as the size of
# the fixed instrument header; the variable-length WF / PW / Filter
# tables start there.
INST_HEADER_SIZE = 0x10
INST_CONTROL_POS = 0x00
INST_HR_AD_POS = 0x01  # hard-restart attack/decay
INST_HR_SR_POS = 0x02  # hard-restart sustain/release
INST_AD_POS = 0x03  # note-start attack/decay
INST_SR_POS = 0x04  # note-start sustain/release
INST_VIBRATO_POS = 0x05  # vibrato freq (low nibble) + amp (high)
INST_VIBRATO_DELAY_POS = 0x06
INST_ARP_SPEED_POS = 0x07
INST_DEFAULT_CHORD_POS = 0x08
INST_OCTAVE_POS = 0x09  # signed 2's-complement semitone shift
INST_PW_TABLE_PTR_POS = 0x0A  # PW-table offset, relative to instbase
INST_FILTER_TABLE_PTR_POS = 0x0B
INST_GATEOFF_WF_POS = 0x0C
INST_GATEOFF_PW_POS = 0x0D
INST_GATEOFF_FILT_POS = 0x0E
INST_FIRST_WAVEFORM_POS = 0x0F  # 1st-frame waveform (see :class:`Waveform`)
INST_WF_TABLE_POS = INST_HEADER_SIZE


class Waveform(IntFlag):
    """SID waveform bits as encoded by SID-Wizard.

    Each flag corresponds to one of the SID's four waveform generators.
    Combine with ``|`` to mix waveforms (e.g. ``Waveform.TRIANGLE |
    Waveform.PULSE`` selects a tied triangle+pulse output). A zero value
    selects silence.
    """

    TRIANGLE = 0x01
    SAWTOOTH = 0x02
    PULSE = 0x04
    NOISE = 0x08


def straight_tempo(frames_per_row: int) -> int:
    """Encode a "straight" (non-funky) subtune-tempo byte.

    ``frames_per_row`` is the player's row delay in PAL frames (1..127);
    the returned byte sets :data:`TEMPO_STRAIGHT_FLAG` so the runtime uses
    it directly instead of averaging with its partner byte in a funktempo
    pair.
    """
    if not (1 <= frames_per_row <= 0x7F):
        raise ValueError("frames_per_row must be 1..127")
    return TEMPO_STRAIGHT_FLAG | frames_per_row
