"""Pure-Python reader, writer, and player for SID-Wizard SWM modules.

SID-Wizard is a Commodore 64 tracker by Hermit (Mihaly Horvath). This
library implements its SWM module format (documented in
``native/sources/SWM-spec.src`` of the SID-Wizard source distribution)
from first principles — no native dependencies, no extension modules.

Public surfaces:

* :func:`read_swm` / :func:`parse_swm` — parse an SWM file or byte
  string into a :class:`SWMFile`.
* :func:`parse_sid` / :func:`read_sid` and :func:`parse_swp` /
  :func:`read_swp` — read SID-Wizard ``.sid`` exports (plain or ``SWP``-packed)
  and standalone ``.swp`` packed tunes into the same :class:`SWMFile`.
* :func:`parse_swi` / :func:`read_swi` and :func:`build_swi` /
  :func:`write_swi` — read the editor's single-instrument ``.swi`` files and
  write them back; round-trip byte-exactly.
* :func:`write_swm` / :func:`build_swm` — serialise an :class:`SWMFile`
  back to disk or bytes; round-trips byte-exactly.
* :class:`SWMPlayer` (in :mod:`pysidwizard.player`) — the playspec
  ``specs/sidwizard.play`` run on the tune's own 6502 image (a bare module's
  SID-Maker export), validated against its 6502 and the ``anarkiwi/sidtrace``
  oracle (``pytest -m oracle``).
"""

from pysidtracker import attack_decay, sustain_release

from .constants import Waveform, straight_tempo
from .errors import SIDFormatError, SWMError, SWMFormatError
from .features import PlayerQuirks
from .model import (
    End,
    Instrument,
    Loop,
    MainVolume,
    Pattern,
    PlayPattern,
    RawSequenceByte,
    Row,
    SequenceCommand,
    SubtuneJump,
    SWMFile,
    TempoOverride,
    Transpose,
    decode_sequence,
    encode_sequence,
    pack_pattern,
    unpack_pattern,
)
from .player import SWMPlayer
from .reader import parse_swm, read_swm
from .reglog import (
    RegWrite,
    iter_register_writes,
    read_reglog,
    write_reglog,
)
from .sidfile import PsidHeader, is_sidwizard_sid, parse_psid_header
from .sidreader import (
    SidWizardSidParser,
    parse_sid,
    parse_sid_song,
    parse_swp,
    read_sid,
    read_swp,
)
from .swi import (
    SWI_DEFAULT_LOAD_ADDRESS,
    build_swi,
    insert_instrument,
    parse_swi,
    read_swi,
    write_swi,
)
from .variant import PlayerImage
from .writer import build_swm, write_swm

__all__ = [
    "End",
    "Instrument",
    "Loop",
    "MainVolume",
    "Pattern",
    "PlayerImage",
    "PlayerQuirks",
    "PlayPattern",
    "PsidHeader",
    "RawSequenceByte",
    "RegWrite",
    "Row",
    "SIDFormatError",
    "SWMError",
    "SWMFile",
    "SWMFormatError",
    "SWMPlayer",
    "SequenceCommand",
    "SubtuneJump",
    "SidWizardSidParser",
    "SWI_DEFAULT_LOAD_ADDRESS",
    "TempoOverride",
    "Transpose",
    "Waveform",
    "attack_decay",
    "build_swi",
    "build_swm",
    "decode_sequence",
    "encode_sequence",
    "insert_instrument",
    "is_sidwizard_sid",
    "iter_register_writes",
    "pack_pattern",
    "parse_psid_header",
    "parse_sid",
    "parse_sid_song",
    "parse_swi",
    "parse_swp",
    "parse_swm",
    "read_reglog",
    "read_sid",
    "read_swm",
    "read_swp",
    "read_swi",
    "straight_tempo",
    "sustain_release",
    "unpack_pattern",
    "write_reglog",
    "write_swi",
    "write_swm",
]
