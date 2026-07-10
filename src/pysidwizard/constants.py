"""SWM file-format constants.

The numeric constants below mirror those in the upstream SID-Wizard source
file ``native/sources/SWM-spec.src``. They are reproduced here so the library
can be used without the C source distribution.
"""

from enum import IntFlag

# Magic identifiers
SWM_MAGIC = b"SWM1"
SWS_MAGIC = b"SWMS"

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
