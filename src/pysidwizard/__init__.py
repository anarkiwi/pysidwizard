"""Pure-Python reader, writer, and player for SID-Wizard SWM modules.

SID-Wizard is a Commodore 64 tracker by Hermit (Mihaly Horvath). This
library implements its SWM module format (documented in
``native/sources/SWM-spec.src`` of the SID-Wizard source distribution)
from first principles — no native dependencies, no extension modules.

Three public surfaces:

* :func:`read_swm` / :func:`parse_swm` — parse an SWM file or byte
  string into a :class:`SWMFile`.
* :func:`write_swm` / :func:`build_swm` — serialise an :class:`SWMFile`
  back to disk or bytes; round-trips byte-exactly.
* :class:`SWMPlayer` (in :mod:`pysidwizard.player`) — per-frame
  emulation of SID-Wizard's 6502 player IRQ. Output matches a real
  SID-Wizard running inside ``asid-vice`` byte-for-byte, every frame ×
  every SID register — verified by the integration test suite on
  every PR.
"""

from .constants import Waveform, attack_decay, straight_tempo, sustain_release
from .errors import SWMError, SWMFormatError
from .model import (
    End,
    Instrument,
    Loop,
    Pattern,
    PlayPattern,
    RawSequenceByte,
    Row,
    SequenceCommand,
    SWMFile,
    TempoOverride,
    Transpose,
    decode_sequence,
    encode_sequence,
    pack_pattern,
    unpack_pattern,
)
from .player import SWMPlayer, iter_writes, render_wav, write_csv
from .reader import parse_swm, read_swm
from .writer import build_swm, write_swm

__all__ = [
    "End",
    "Instrument",
    "Loop",
    "Pattern",
    "PlayPattern",
    "RawSequenceByte",
    "Row",
    "SWMError",
    "SWMFile",
    "SWMFormatError",
    "SWMPlayer",
    "SequenceCommand",
    "TempoOverride",
    "Transpose",
    "Waveform",
    "attack_decay",
    "build_swm",
    "decode_sequence",
    "encode_sequence",
    "iter_writes",
    "pack_pattern",
    "parse_swm",
    "read_swm",
    "render_wav",
    "straight_tempo",
    "sustain_release",
    "unpack_pattern",
    "write_csv",
    "write_swm",
]
