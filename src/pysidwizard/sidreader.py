"""Read SID-Wizard-authored single-SID PSID (``.sid``) files.

This is a **read-only, lossy** companion to :mod:`pysidwizard.reader`. Where
``parse_swm`` reads SID-Wizard's editor workfile (``.swm``), :func:`parse_sid`
recovers a playable :class:`~pysidwizard.model.SWMFile` from the *runtime*
tune image that SID-Wizard's exporter bakes into a ``.sid`` file. The result
drives :class:`~pysidwizard.player.SWMPlayer` / ``render_wav`` unchanged.

It is lossy on purpose: the exporter discards instrument names and the
editor's per-pattern row-length is not authoritatively recoverable, so this
module synthesises placeholder instrument names and best-effort lengths. Do
**not** route the result back through the writer expecting a byte-exact
round-trip.

Scope: single-SID ``PSID`` and ``RSID`` tunes, including SID-Wizard's
*packed* (``SWP``) exports. Multi-subtune tunes are fully decoded (each
subtune is reachable via :meth:`SWMFile.subtune`). Out of scope, rejected
with a clear, specific error: multi-SID files (the player is single-SID
only), and the occasional relocated/loader layout that hides the tune
tables entirely.

Two on-disk layouts are decoded; the byte-level decoding of patterns,
instruments, sequences and chord/tempo tables is identical between them and
shared. They differ only in how the pointer-table bases are located and
whether the pointers they hold are absolute or relative.

Plain (uncompressed) layout
---------------------------
The exporter copies the 64-byte SWM tune header verbatim into the player's
variable area (so it survives as ``SWM1`` in the file) and lays the fully
expanded tune data out in memory as, low address to high::

    sequences | patterns | instruments | chordtable | tempotable |
    subtunes | tempo-ptrs | chord-ptrs | inst-lo | inst-hi | ptn-lo | ptn-hi

All pointers are absolute C64 addresses. ``ptn-hi`` is the last region, so its
end is the end of the tune data (``expoendadd``). The pointer-table bases are
recovered backward from that end using the exporter's exact arithmetic::

    ptn_hi = end - (patterns + 1)
    ptn_lo = ptn_hi - patterns
    inst_hi = ptn_lo - instruments
    inst_lo = inst_hi - (instruments + 1)

The subtune table (3 channel sequence-pointers + a funktempo pair per
subtune, 8 bytes each for one SID) sits just below ``inst_lo`` separated by
the chord/tempo pointer tables; it is located by scanning for the block whose
sequence pointers fall in the (low-address) sequences region. The chord and
tempo tables are then derived from the subtune base.

Packed (``SWP``) layout
-----------------------
"SWP-packed" is a misnomer: the tune data is **not** compressed. It is the
same compact runtime layout, with two differences. First, the pointer tables
hold pointers **relative** to a base (the C64 address of the ``SWP1`` magic;
the player adds it at init). Second, the table bases are not recovered by
scanning from end-of-data but read from an offset-table inside the ``SWP1``
header: nine little-endian 16-bit offsets (relative to that same base) at
fixed byte positions — subtunes, ptn-lo/hi, inst-lo/hi, chord table/ptrs and
tempo table/ptrs (see :data:`_SWP_TABLE_OFFSETS`).

The structural counts come from the table extents (the tables are laid out
contiguously, so e.g. ``patterns = ptn_hi_off - ptn_lo_off`` and
``subtunes = (next_off - subtunes_off) // 8``), not from the ``SWM1`` header:
some packed exports ship a stale player-template ``SWM1`` header (placeholder
counts, author ``"SIW-WIZARD"``) or omit it entirely. When an ``SWM1`` header
is present it is still used for the lossy metadata (frame speed, author,
driver/tuning, chord/tempo-table lengths); otherwise model defaults are used.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional, Union

from pysidtracker import BaseSidParser, CodePattern, SidImage, find_code_all

from .constants import (
    AUTHOR_LEN,
    AUTHOR_POS,
    AUTO_POS,
    CHORD_LENGTH_POS,
    COLOR_THEME_POS,
    CONFIG_BITS_POS,
    DEFAULT_PATTERN_LEN_POS,
    DRIVER_TYPE_POS,
    FRAMESPEED_POS,
    HIGHLIGHT_POS,
    INST_FILTER_TABLE_PTR_POS,
    INSTRUMENT_AMOUNT_POS,
    KEYBOARD_TYPE_POS,
    MUTE_POS,
    PACKED_MAX,
    PACKED_MIN,
    PATTERN_AMOUNT_POS,
    RESERVED_POS,
    SEQUENCE_AMOUNT_POS,
    SID_CHANNELS,
    SWM_MAGIC,
    TABLE_END,
    TEMPO_LENGTH_POS,
    TUNE_HEADER_SIZE,
    TUNING_TYPE_POS,
)
from .errors import SIDFormatError
from .model import (
    Instrument,
    Pattern,
    PlayPattern,
    SWMFile,
    decode_sequence,
    unpack_pattern,
)
from .sidfile import PSID_MAGIC, RSID_MAGIC, find_player_signature, parse_psid_header

# SID-Wizard's "packed" (``SWP``) export keeps an ``SWP1`` magic (the ``SWM1``
# header with its ``M`` turned into ``P``). Despite the name the tune data is
# not compressed: it is the same runtime layout with relative pointers located
# via an offset-table in the ``SWP1`` header (see the module docstring and
# :func:`_build_swp`).
SWP_MAGIC = b"SWP1"

# Byte positions, within the ``SWP1`` block, of the nine little-endian 16-bit
# table-base offsets (each relative to the C64 address of the ``SWP1`` magic).
# Mirrors SID-Wizard's ``settings.cfg`` packer layout.
_SWP_TABLE_OFFSETS = {
    "subtunes": 4,
    "ptn_lo": 6,
    "ptn_hi": 8,
    "inst_lo": 10,
    "inst_hi": 12,
    "chord_table": 14,
    "chord_ptr": 16,
    "tempo_table": 18,
    "tempo_ptr": 20,
}

# Per-subtune block in the runtime "subtunes" table: SID_CHANNELS 16-bit
# sequence pointers followed by a [left, right] funktempo pair.
_SUBTUNE_BLOCK = 2 * SID_CHANNELS + 2  # 8 for one SID
# The exporter shifts the tempo table forward by (2 + 2*CHN_AMOUNT) bytes to
# reserve room for the selected subtune tempos (the "RESTEMP" area).
_RESTEMP_SHIFT = 2 + 2 * SID_CHANNELS  # 8 for one SID
# How far below inst_lo to scan for the subtune table (covers the chord- and
# tempo-pointer tables, whose combined size is bounded by the editor limits).
_SUBTUNE_SCAN_WINDOW = 320
# Per-pattern / per-instrument / per-sequence walk guards (defensive bounds).
_INSTRUMENT_TABLE_GUARD = 0x200
_PATTERN_GUARD = 0x400
_SEQUENCE_GUARD = 0x400

_SEQUENCE_END = 0xFE
_SEQUENCE_END_WITH_LOOP = 0xFF

# --- Code-scan (relocation-invariant) table location ---------------------------
# SID-Wizard's player relocates as one block with its table base addresses baked
# into instruction operands. These masked 6502 patterns locate each table-access
# instruction and capture the absolute operand, so the tables are found without
# trusting the ``SWM1`` header, the end-of-data placement, or the load address.
# This is what recovers relocated ``SWM1`` exports (varied load addrs) and the
# magic-less exports (no ``SWM1``/``SWP1``), which the header/end-probe path in
# :func:`_build` cannot resolve. Idioms are mapped from the vendored SID-Wizard
# player source (``native/sources/include/player.asm``): ``p_ptnl1``/``p_ptnh1``
# and ``p_insl3``/``p_insh3`` (``ldy CURx,x; lda PTRLO,y; sta zp; lda PTRHI,y;
# sta zp``), ``p_subt1`` (``adc #imm; tay; lda SUBTUNES,y``), ``p_chdt1``
# (``lda CHORDS,y; cmp #$7E``), ``p_chdp1``/``p_tmpp1`` (``tay; lda TBL,y``),
# ``p_tmpt1`` (``sec; sbc TEMPOTBL-1,y``). The magic-less player-code signature
# that anchors detection lives in :mod:`pysidwizard.sidfile`.
# Pattern-LO/HI and instrument-LO/HI pointer tables share this skeleton; the two
# real matches are told apart by address order (the instrument tables sit below
# the pattern tables) and validated by decoding.
_CODE_PTRTAB = CodePattern("BC ?? ?? B9 {lo:w} 85 ?? B9 {hi:w} 85 ??")
_CODE_SUBTUNES = CodePattern("69 ?? A8 B9 {subt:w}")
_CODE_CHORDTAB = CodePattern("BC ?? ?? B9 {chdt:w} C9 7E D0")
_CODE_CHORDPTR = CodePattern("A8 B9 {chdp:w} 9D")
_CODE_TEMPOPTR = CodePattern("A8 B9 {tmpp:w} 4C")
_CODE_TEMPOTAB = CodePattern("38 F9 {tmpt:w} F0")
# SID-Wizard's canonical player/assembly origin. A partially-relocated export
# leaves some pointer-table entries at their original (pre-relocation) address;
# ``load - _ORIG_LOAD`` shifts such an entry onto the physically relocated data.
_ORIG_LOAD = 0x1000


class _LayoutError(SIDFormatError):
    """Internal: a candidate end-of-data offset did not yield a coherent tune.

    Raised while probing for the true ``expoendadd``; the prober catches it
    and tries the next candidate. The final failure re-raises the last one.
    """


def _validate_pattern_refs(sequences, n_patterns: int) -> None:
    """Reject a layout whose orderlists reference a non-existent pattern.

    Every :class:`PlayPattern` in a coherent tune names a pattern in
    ``1..n_patterns`` (``0`` is the reserved empty slot). A reference above
    ``n_patterns`` means the subtune/sequence pointers were resolved against the
    wrong region (e.g. a wrong end-of-data candidate), so the decoded sequences
    are noise walked out of pattern/instrument bytes. Raising here lets the
    :func:`parse_sid` end-probe reject this candidate and try another — turning
    a silent mis-parse into either the correct layout or a clean failure.
    """
    for seq in sequences:
        for cmd in seq:
            if isinstance(cmd, PlayPattern) and cmd.pattern > n_patterns:
                raise _LayoutError(
                    f"orderlist references pattern {cmd.pattern} but only {n_patterns} exist"
                )


def parse_sid(data: bytes) -> SWMFile:
    """Parse a single-SID SID-Wizard ``.sid`` image into a :class:`SWMFile`.

    The returned model plays under :class:`~pysidwizard.player.SWMPlayer`
    exactly as a parsed ``.swm`` would (multi-subtune tunes expose their other
    subtunes via :meth:`SWMFile.subtune`). Read-only and lossy (see the module
    docstring).

    Both ``PSID`` and ``RSID`` containers are accepted: RSID uses a different
    init/play convention but the embedded SID-Wizard tune-data layout is
    identical, so any RSID whose tables resolve is parsed too. Plain and
    *packed* (``SWP``) exports are both decoded. Raises :class:`SIDFormatError`
    for: an unrecognised magic, a multi-SID file (the player and model are
    single-SID only, so playback would be wrong), a missing tune header on a
    plain export, truncation, or pointer tables that do not resolve.
    """
    header = parse_psid_header(data)
    if header.magic not in (PSID_MAGIC, RSID_MAGIC):
        raise SIDFormatError(f"unsupported SID magic {header.magic!r}")
    if header.version >= 3 or header.is_multi_sid:
        raise SIDFormatError(
            "multi-SID (2-SID / 3-SID) files are not supported: the player and "
            "SWMFile model are single-SID only, so parsing one SID's channels "
            "would misrepresent the tune"
        )

    load = header.real_load_address
    base = header.data_start  # file offset that maps to ``load``
    swp_pos = data.find(SWP_MAGIC, base)
    if swp_pos >= 0:
        # A "packed" (SWP) export: relative-pointer tables located via the
        # SWP1 offset-table. Not compressed; decoded directly (see _build_swp).
        try:
            return _build_swp(data, load, base, swp_pos)
        except _LayoutError as exc:
            raise SIDFormatError(f"SID-Wizard packed (SWP) tables did not resolve: {exc}") from exc
    swm_pos = data.find(SWM_MAGIC, base)
    if swm_pos < 0:
        # No ``SWM1`` / ``SWP1`` magic: a magic-less (stripped / relocated)
        # export. Recover it by reading the table bases straight from the
        # player-code operands, anchored on the SID-Wizard 1.x player signature.
        try:
            return _build_codescan(data, load, base)
        except _LayoutError as exc:
            raise SIDFormatError(
                "no 'SWM1' tune header found and the SID-Wizard player tables "
                f"could not be located from the player code: {exc}"
            ) from exc
    if swm_pos + TUNE_HEADER_SIZE > len(data):
        raise SIDFormatError("'SWM1' tune header is truncated")
    swm_header = data[swm_pos : swm_pos + TUNE_HEADER_SIZE]

    file_end = load + (len(data) - base)
    # The pattern-hi pointer table ends the tune data, but some files append a
    # small relocation/player stub after it. Probe end-of-data downward from
    # the file end; the first fully coherent layout wins.
    last_error: Optional[SIDFormatError] = None
    for end in range(file_end, load, -1):
        try:
            return _build(data, load, base, swm_header, end)
        except _LayoutError as exc:
            last_error = exc
    # The header end-probe assumes an in-place absolute-pointer layout. When it
    # fails the export is relocated / alternate-layout: fall back to reading the
    # table bases from the player-code operands (relocation-invariant), which
    # also repairs partially-relocated pointer tables.
    try:
        return _build_codescan(data, load, base)
    except _LayoutError:
        pass
    # Neither the in-place end-probe nor the code scan resolved a coherent
    # layout: a stale/uninitialised player-template header (placeholder counts)
    # or an export whose tune data is not materialised statically.
    raise SIDFormatError(
        "'SWM1' tune header found but its pointer tables did not resolve to a "
        "coherent layout (stale/template header or unmaterialised relocated "
        f"export); out of the single-SID direct-load scope. Last probe: {last_error}"
    )


def read_sid(path: Union[str, os.PathLike[str]]) -> SWMFile:
    """Read a SID-Wizard ``.sid`` file from ``path`` and return a :class:`SWMFile`."""
    with open(path, "rb") as fh:
        return parse_sid(fh.read())


class SidWizardSidParser(BaseSidParser):
    """:class:`pysidtracker.BaseSidParser` adapter for SID-Wizard ``.sid`` tunes.

    Gives SID-Wizard the shared ``read``/``parse``/``detect`` surface: parsing
    delegates to :func:`parse_sid`, and :meth:`recognize` anchors on the
    ``SWM1`` tune header, the ``SWP1`` packed-export magic, or — for magic-less
    exports — the SID-Wizard 1.x player-code signature, so
    :meth:`~pysidtracker.BaseSidParser.detect` classifies a direct-load tune as
    :attr:`~pysidtracker.PlayroutineKind.DIRECT`.
    """

    error_class = SIDFormatError

    def parse(self, data: bytes, **kwargs: Any) -> SWMFile:
        return parse_sid(data, **kwargs)

    def recognize(self, image: SidImage) -> object:
        pos = image.find(SWM_MAGIC)
        if pos < 0:
            pos = image.find(SWP_MAGIC)
        if pos >= 0:
            return pos
        return find_player_signature(image)


def _build(
    data: bytes,
    load: int,
    base: int,
    swm_header: bytes,
    end: int,
) -> SWMFile:
    """Try to decode the tune assuming the data ends at C64 address ``end``.

    Raises :class:`_LayoutError` if any structure is inconsistent with that
    assumption, so :func:`parse_sid` can try a different ``end``.
    """
    seq_amount = swm_header[SEQUENCE_AMOUNT_POS]
    pat_amount = swm_header[PATTERN_AMOUNT_POS]
    inst_amount = swm_header[INSTRUMENT_AMOUNT_POS]
    chord_len = swm_header[CHORD_LENGTH_POS]
    tempo_len = swm_header[TEMPO_LENGTH_POS]
    subtunes = (seq_amount - 1) // SID_CHANNELS + 1 if seq_amount > 0 else 1

    byte, chunk = _memory_accessors(data, load, base)

    # Pointer-table bases, derived backward from end-of-data (exporter math).
    ptn_hi = end - (pat_amount + 1)
    ptn_lo = ptn_hi - pat_amount
    inst_hi = ptn_lo - inst_amount
    inst_lo = inst_hi - (inst_amount + 1)
    if inst_lo <= load:
        raise _LayoutError("pointer tables underflow the load address")

    instruments = _decode_instruments(byte, chunk, inst_lo, inst_hi, inst_amount, load, end)
    patterns, min_pattern = _decode_patterns(byte, chunk, ptn_lo, ptn_hi, pat_amount, load, end)

    subtune_base = _locate_subtune_table(byte, inst_lo, subtunes, seq_amount, load, min_pattern)
    sequences = _decode_sequences(byte, chunk, subtune_base, seq_amount, load, end)
    _validate_pattern_refs(sequences, len(patterns))
    subtune_tempos = [
        (
            byte(subtune_base + _SUBTUNE_BLOCK * st + 2 * SID_CHANNELS),
            byte(subtune_base + _SUBTUNE_BLOCK * st + 2 * SID_CHANNELS + 1),
        )
        for st in range(subtunes)
    ]

    # Chord/tempo tables sit immediately below the subtune table, with the
    # 8-byte RESTEMP gap the exporter inserts ahead of the tempo programs.
    chord_base = subtune_base - _RESTEMP_SHIFT - tempo_len - chord_len
    if chord_base <= min_pattern:
        raise _LayoutError("chord table overlaps the pattern region")
    chord_table = chunk(chord_base, chord_len) if chord_len else b""
    tempo_table = chunk(chord_base + chord_len + _RESTEMP_SHIFT, tempo_len) if tempo_len else b""

    return _assemble(
        swm_header,
        load,
        sequences,
        patterns,
        instruments,
        chord_table,
        tempo_table,
        subtune_tempos,
    )


def _memory_accessors(data: bytes, load: int, base: int):
    """Return ``(byte, chunk)`` closures mapping C64 addresses into ``data``.

    ``byte(addr)`` reads a single byte and ``chunk(addr, length)`` a slice,
    both translating the C64 address to a file offset via ``addr - load +
    base`` and raising :class:`_LayoutError` on an out-of-range access.
    """

    def byte(addr: int) -> int:
        off = addr - load + base
        if off < 0 or off >= len(data):
            raise _LayoutError(f"address {addr:#06x} out of range")
        return data[off]

    def chunk(addr: int, length: int) -> bytes:
        off = addr - load + base
        if off < 0 or off + length > len(data):
            raise _LayoutError(f"slice at {addr:#06x}+{length} out of range")
        return bytes(data[off : off + length])

    return byte, chunk


def _assemble(
    swm_header: Optional[bytes],
    load: int,
    sequences,
    patterns,
    instruments,
    chord_table: bytes,
    tempo_table: bytes,
    subtune_tempos,
) -> SWMFile:
    """Build the :class:`SWMFile` from decoded regions and the tune header.

    ``swm_header`` carries the lossy metadata (frame speed, author, driver /
    tuning type, ...). It is ``None`` for a packed export that omits the
    ``SWM1`` header, in which case the model's own field defaults are kept.
    """
    meta = {}
    if swm_header is not None:
        meta = dict(
            frame_speed=swm_header[FRAMESPEED_POS],
            highlight=swm_header[HIGHLIGHT_POS],
            auto_advance=swm_header[AUTO_POS],
            config_bits=swm_header[CONFIG_BITS_POS],
            mute=bytes(swm_header[MUTE_POS : MUTE_POS + 3]),
            default_pattern_length=swm_header[DEFAULT_PATTERN_LEN_POS],
            color_theme=swm_header[COLOR_THEME_POS],
            keyboard_type=swm_header[KEYBOARD_TYPE_POS],
            driver_type=swm_header[DRIVER_TYPE_POS],
            tuning_type=swm_header[TUNING_TYPE_POS],
            reserved=bytes(swm_header[RESERVED_POS:AUTHOR_POS]),
            author=bytes(swm_header[AUTHOR_POS : AUTHOR_POS + AUTHOR_LEN]),
        )
    return SWMFile(
        sequences=sequences,
        patterns=patterns,
        instruments=instruments,
        chord_table=chord_table,
        tempo_table=tempo_table,
        subtune_tempos=subtune_tempos,
        load_address=load,
        **meta,
    )


def _build_swp(data: bytes, load: int, base: int, swp_off: int) -> SWMFile:
    """Decode a "packed" (``SWP``) export.

    The tune data is the ordinary runtime layout; only the pointer-table
    bases and pointer values differ. The bases are read from the ``SWP1``
    offset-table (:data:`_SWP_TABLE_OFFSETS`) and every table entry is a
    pointer relative to ``swp_base`` (the C64 address of the ``SWP1`` magic).

    Raises :class:`SIDFormatError` if the offset-table or any resolved table
    does not yield a coherent tune.
    """
    swp_base = load + (swp_off - base)
    byte, chunk = _memory_accessors(data, load, base)
    end = load + (len(data) - base)  # one past the last addressable byte

    def swp_u16(pos: int) -> int:
        off = swp_off + pos
        if off + 1 >= len(data):
            raise _LayoutError("SWP1 offset-table is truncated")
        return data[off] | (data[off + 1] << 8)

    offs = {name: swp_u16(pos) for name, pos in _SWP_TABLE_OFFSETS.items()}

    # Structural counts come from the (contiguous) table extents, not the
    # SWM1 header: packed exports may ship a stale template header or none.
    sub_off = offs["subtunes"]
    higher = [v for v in offs.values() if v > sub_off]
    if not higher:
        raise _LayoutError("SWP subtune table has no following table")
    subtunes = (min(higher) - sub_off) // _SUBTUNE_BLOCK
    inst_amount = offs["ptn_lo"] - offs["inst_hi"]
    pat_amount = offs["ptn_hi"] - offs["ptn_lo"]
    if subtunes < 1 or inst_amount < 0 or pat_amount < 0:
        raise _LayoutError(
            f"implausible SWP counts: subtunes={subtunes}, "
            f"instruments={inst_amount}, patterns={pat_amount}"
        )
    seq_amount = subtunes * SID_CHANNELS

    # The SWM1 header, when present, supplies the lossy metadata and the
    # chord/tempo-table lengths. It is optional for packed tunes.
    swm_pos = data.find(SWM_MAGIC, base)
    if 0 <= swm_pos and swm_pos + TUNE_HEADER_SIZE <= len(data):
        swm_header: Optional[bytes] = data[swm_pos : swm_pos + TUNE_HEADER_SIZE]
        chord_len = swm_header[CHORD_LENGTH_POS]
        tempo_len = swm_header[TEMPO_LENGTH_POS]
    else:
        swm_header = None
        chord_len = tempo_len = 0

    inst_lo = swp_base + offs["inst_lo"]
    inst_hi = swp_base + offs["inst_hi"]
    ptn_lo = swp_base + offs["ptn_lo"]
    ptn_hi = swp_base + offs["ptn_hi"]
    subtune_base = swp_base + offs["subtunes"]

    instruments = _decode_instruments(
        byte, chunk, inst_lo, inst_hi, inst_amount, load, end, ptr_base=swp_base
    )
    patterns, _ = _decode_patterns(
        byte, chunk, ptn_lo, ptn_hi, pat_amount, load, end, ptr_base=swp_base
    )
    sequences = _decode_sequences(
        byte, chunk, subtune_base, seq_amount, load, end, ptr_base=swp_base
    )
    _validate_pattern_refs(sequences, len(patterns))
    subtune_tempos = [
        (
            byte(subtune_base + _SUBTUNE_BLOCK * st + 2 * SID_CHANNELS),
            byte(subtune_base + _SUBTUNE_BLOCK * st + 2 * SID_CHANNELS + 1),
        )
        for st in range(subtunes)
    ]

    chord_table = chunk(swp_base + offs["chord_table"], chord_len) if chord_len else b""
    tempo_table = chunk(swp_base + offs["tempo_table"], tempo_len) if tempo_len else b""

    return _assemble(
        swm_header,
        load,
        sequences,
        patterns,
        instruments,
        chord_table,
        tempo_table,
        subtune_tempos,
    )


def _resolve_ptr(raw, ptr_base, load, end, reloc_delta):
    """Map a raw 16-bit table pointer to a physical C64 address.

    ``ptr_base`` is added first (the ``SWP`` relative-pointer base, ``0`` for
    absolute layouts). ``reloc_delta`` repairs a partially-relocated export: a
    pointer that does not land inside the loaded image is re-interpreted as an
    original (pre-relocation) address and shifted by ``reloc_delta`` so it maps
    onto the physically relocated data. ``reloc_delta == 0`` is a no-op.
    """
    addr = ptr_base + raw
    if reloc_delta and not (load <= addr < end):
        addr = ptr_base + raw + reloc_delta
    return addr


def _decode_instruments(
    byte, chunk, inst_lo, inst_hi, inst_amount, load, end, ptr_base=0, reloc_delta=0
):
    """Decode the ``inst_amount`` instruments (1-based; index 0 is the dummy).

    Each instrument is a 16-byte header followed by three inline ``$FF``-
    terminated tables (waveform / pulse-width / filter). The body runs from
    the pointer to the filter table's terminating ``$FF`` — exactly the span
    the player reads — and is handed to :meth:`Instrument.decode`.
    """
    instruments: List[Instrument] = []
    for k in range(1, inst_amount + 1):
        raw = byte(inst_lo + k) | (byte(inst_hi + k) << 8)
        addr = _resolve_ptr(raw, ptr_base, load, end, reloc_delta)
        if not (load <= addr < end):
            raise _LayoutError(f"instrument {k} pointer {addr:#06x} out of range")
        # The filter-table pointer is relative to the instrument base; walk to
        # its ``$FF`` terminator to find where the body ends.
        cursor = addr + byte(addr + INST_FILTER_TABLE_PTR_POS)
        guard = 0
        while byte(cursor) != TABLE_END:
            cursor += 1
            guard += 1
            if guard > _INSTRUMENT_TABLE_GUARD:
                raise _LayoutError(f"instrument {k} filter table unterminated")
        try:
            instruments.append(Instrument.decode(chunk(addr, cursor - addr), name=b"INST %02d" % k))
        except _LayoutError:
            raise
        except Exception as exc:  # SWMFormatError from a malformed body
            raise _LayoutError(f"instrument {k} did not decode: {exc}") from exc
    return instruments


def _scan_pattern_end(byte, start: int) -> int:
    """Return the address of a pattern's ``$FF`` terminator.

    Walks the NOP-packed pattern stream row by row so that an ``$FF`` byte
    appearing as effect data mid-row is not mistaken for the terminator (only
    an ``$FF`` at a row boundary ends the pattern).
    """
    addr = start
    guard = 0
    while True:
        b = byte(addr)
        if b == TABLE_END:
            return addr
        addr += 1
        guard += 1
        if guard > _PATTERN_GUARD:
            raise _LayoutError("pattern not terminated")
        if b == 0x00:
            # An empty row, possibly followed by packed-NOP run markers.
            while PACKED_MIN <= byte(addr) <= PACKED_MAX:
                addr += 1
            continue
        if b & 0x80:  # note column carries an instrument column
            inst_byte = byte(addr)
            addr += 1
            if inst_byte & 0x80:  # ... and an fx column
                fx = byte(addr)
                addr += 1
                if fx < 0x20:  # "big" fx carries a value byte
                    addr += 1


def _decode_patterns(byte, chunk, ptn_lo, ptn_hi, pat_amount, load, end, ptr_base=0, reloc_delta=0):
    """Decode the ``pat_amount`` patterns (1-based; index 0 is the reserved slot)."""
    patterns: List[Pattern] = []
    min_pattern = end
    for k in range(1, pat_amount + 1):
        raw = byte(ptn_lo + k) | (byte(ptn_hi + k) << 8)
        addr = _resolve_ptr(raw, ptr_base, load, end, reloc_delta)
        if not (load <= addr < end):
            raise _LayoutError(f"pattern {k} pointer {addr:#06x} out of range")
        min_pattern = min(min_pattern, addr)
        term = _scan_pattern_end(byte, addr)
        unpacked = unpack_pattern(chunk(addr, term - addr))
        # The original pattern length survives as the byte after the
        # terminator (the exporter only overwrites the size byte); fall back to
        # the decoded row count if it looks implausible.
        length = byte(term + 1)
        if not 0 < length <= 0xFF:
            length = len(unpacked)
        patterns.append(Pattern.decode(unpacked, length=length))
    return patterns, min_pattern


def _locate_subtune_table(byte, inst_lo, subtunes, seq_amount, load, min_pattern):
    """Find the subtune-table base just below ``inst_lo``.

    The chord- and tempo-pointer tables sit between the subtune table and
    ``inst_lo`` and their sizes are not in the header, so scan downward for the
    highest block whose ``seq_amount`` sequence pointers all land in the
    sequences region (i.e. below the lowest pattern). The chord/tempo pointer
    tables point *into* the high chord/tempo tables, so they never satisfy this
    test — making the first match the genuine subtune table.
    """
    top = inst_lo - _SUBTUNE_BLOCK * subtunes
    floor = max(load, top - _SUBTUNE_SCAN_WINDOW)
    for candidate in range(top, floor - 1, -1):
        if _subtune_pointers_valid(byte, candidate, seq_amount, load, min_pattern):
            return candidate
    raise _LayoutError("could not locate the subtune table")


def _subtune_block_addr(base: int, seq_index: int) -> int:
    subtune, channel = divmod(seq_index, SID_CHANNELS)
    return base + _SUBTUNE_BLOCK * subtune + 2 * channel


def _subtune_pointers_valid(
    byte, base, seq_amount, load, min_pattern, end=None, reloc_delta=0
) -> bool:
    scan_end = end if end is not None else min_pattern
    for k in range(seq_amount):
        blk = _subtune_block_addr(base, k)
        try:
            raw = byte(blk) | (byte(blk + 1) << 8)
        except _LayoutError:
            return False
        ptr = _resolve_ptr(raw, 0, load, scan_end, reloc_delta)
        if not (load <= ptr < min_pattern):
            return False
    return True


def _decode_sequences(byte, chunk, subtune_base, seq_amount, load, end, ptr_base=0, reloc_delta=0):
    """Decode the ``seq_amount`` channel sequences via the subtune table.

    Sequence ``k`` lives in subtune ``k // 3`` channel ``k % 3``; each runs
    from its pointer to the ``$FE`` (end) or ``$FF`` + loop-position-byte
    terminator, matching the runtime player.
    """
    sequences: List[List] = []
    for k in range(seq_amount):
        blk = _subtune_block_addr(subtune_base, k)
        raw = byte(blk) | (byte(blk + 1) << 8)
        start = _resolve_ptr(raw, ptr_base, load, end, reloc_delta)
        if not (load <= start < end):
            raise _LayoutError(f"sequence {k} pointer {start:#06x} out of range")
        addr = start
        guard = 0
        while True:
            command = byte(addr)
            addr += 1
            guard += 1
            if guard > _SEQUENCE_GUARD:
                raise _LayoutError(f"sequence {k} not terminated")
            if command == _SEQUENCE_END:
                break
            if command == _SEQUENCE_END_WITH_LOOP:
                addr += 1  # consume the loop-position byte
                break
        sequences.append(decode_sequence(chunk(start, addr - start)))
    return sequences


def _code_operands(image: SidImage, pattern: CodePattern, key: str) -> list:
    """Sorted unique captured operands of ``pattern`` in ``image``."""
    return sorted({m.captures[key] for m in find_code_all(image, pattern)})


def _iter_subtune_bases(byte, image, inst_lo, min_pattern, load, end, seq_hint, chdp, tmpp, reloc):
    """Yield ``(subtune_base, subtunes)`` candidates for the sequence-pointer table.

    A code-located base (``p_subt1``) is offered first; then every position in
    the scan window below ``inst_lo`` whose sequence pointers land in the
    sequences region. The caller decodes each and keeps the first whose
    orderlist pattern references all resolve, so a false-positive block is
    skipped rather than mis-parsed.
    """
    seen = set()
    for sb in _code_operands(image, _CODE_SUBTUNES, "subt"):
        uppers = [x for x in ([inst_lo] + chdp + tmpp) if x > sb]
        if not uppers:
            continue
        n = seq_hint or ((min(uppers) - sb) // _SUBTUNE_BLOCK)
        if n >= 1 and (sb, n) not in seen:
            seen.add((sb, n))
            yield sb, n
    counts = [seq_hint] if seq_hint else list(range(8, 0, -1))
    for n in counts:
        if not n or n < 1:
            continue
        seq_amount = n * SID_CHANNELS
        top = inst_lo - _SUBTUNE_BLOCK * n
        floor = max(load, top - _SUBTUNE_SCAN_WINDOW)
        for sb in range(top, floor - 1, -1):
            if (sb, n) in seen:
                continue
            if _subtune_pointers_valid(byte, sb, seq_amount, load, min_pattern, end, reloc):
                seen.add((sb, n))
                yield sb, n


def _codescan_chord_tempo(image, chunk, subtune_base, min_pattern, chord_len, tempo_len):
    """Return ``(chord_table, tempo_table)`` bytes for a code-scanned layout.

    Prefers the code-located table bases (``p_chdt1`` / ``p_tmpt1``); falls back
    to the geometry below the subtune table (as :func:`_build` does) for either
    table the code scan did not pin uniquely.
    """
    chord_table = tempo_table = b""
    chdt = _code_operands(image, _CODE_CHORDTAB, "chdt")
    tmpt = _code_operands(image, _CODE_TEMPOTAB, "tmpt")
    if chord_len > 0 and len(chdt) == 1:
        try:
            chord_table = chunk(chdt[0], chord_len)
        except _LayoutError:
            chord_table = b""
    if tempo_len > 0 and len(tmpt) == 1:
        try:
            tempo_table = chunk(tmpt[0] + 1, tempo_len)
        except _LayoutError:
            tempo_table = b""
    if (chord_len > 0 and not chord_table) or (tempo_len > 0 and not tempo_table):
        chord_base = subtune_base - _RESTEMP_SHIFT - tempo_len - chord_len
        if chord_base > min_pattern:
            if chord_len > 0 and not chord_table:
                try:
                    chord_table = chunk(chord_base, chord_len)
                except _LayoutError:
                    pass
            if tempo_len > 0 and not tempo_table:
                try:
                    tempo_table = chunk(chord_base + chord_len + _RESTEMP_SHIFT, tempo_len)
                except _LayoutError:
                    pass
    return chord_table, tempo_table


def _try_codescan_layout(
    image,
    byte,
    chunk,
    load,
    end,
    ins_pair,
    pat_pair,
    seq_hint,
    chord_len,
    tempo_len,
    swm_header,
    chdp,
    tmpp,
    reloc,
):
    """Decode one candidate ``(instrument, pattern)`` table pairing.

    Counts come from the table extents baked into the player code
    (``pat_amount = ptn_hi - ptn_lo``, ``inst_amount = inst_hi - inst_lo - 1``),
    so no header count is trusted. Raises :class:`_LayoutError` if the pairing
    is not coherent.
    """
    inst_lo, inst_hi = ins_pair
    ptn_lo, ptn_hi = pat_pair
    inst_amount = inst_hi - inst_lo - 1
    pat_amount = ptn_hi - ptn_lo
    if inst_amount < 0 or pat_amount < 0 or inst_lo <= load or ptn_lo <= load:
        raise _LayoutError("code-scan: implausible table geometry")
    instruments = _decode_instruments(
        byte, chunk, inst_lo, inst_hi, inst_amount, load, end, reloc_delta=reloc
    )
    patterns, min_pattern = _decode_patterns(
        byte, chunk, ptn_lo, ptn_hi, pat_amount, load, end, reloc_delta=reloc
    )
    last: Optional[_LayoutError] = None
    for subtune_base, subtunes in _iter_subtune_bases(
        byte, image, inst_lo, min_pattern, load, end, seq_hint, chdp, tmpp, reloc
    ):
        seq_amount = subtunes * SID_CHANNELS
        try:
            sequences = _decode_sequences(
                byte, chunk, subtune_base, seq_amount, load, end, reloc_delta=reloc
            )
            _validate_pattern_refs(sequences, len(patterns))
        except _LayoutError as exc:
            last = exc
            continue
        subtune_tempos = [
            (
                byte(subtune_base + _SUBTUNE_BLOCK * st + 2 * SID_CHANNELS),
                byte(subtune_base + _SUBTUNE_BLOCK * st + 2 * SID_CHANNELS + 1),
            )
            for st in range(subtunes)
        ]
        chord_table, tempo_table = _codescan_chord_tempo(
            image, chunk, subtune_base, min_pattern, chord_len, tempo_len
        )
        return _assemble(
            swm_header,
            load,
            sequences,
            patterns,
            instruments,
            chord_table,
            tempo_table,
            subtune_tempos,
        )
    raise last or _LayoutError("code-scan: no coherent subtune table")


def _build_codescan(data: bytes, load: int, base: int) -> SWMFile:
    """Decode a tune by reading the table bases from the player code operands.

    Relocation-invariant fallback used when the ``SWM1`` header end-probe cannot
    resolve the layout (relocated / alternate-layout exports) and for magic-less
    exports that carry no ``SWM1`` / ``SWP1`` at all. The pattern- and
    instrument-pointer table bases (and their entry counts) come from the
    player's own indexed-load instructions; the subtune table, chord/tempo
    tables follow. A partially-relocated export whose table entries still hold
    original (pre-relocation) addresses is repaired with the ``load - 0x1000``
    delta. Raises :class:`_LayoutError` if no coherent, playable layout is found.
    """
    image = SidImage.from_sid(data)
    byte, chunk = _memory_accessors(data, load, base)
    end = load + (len(data) - base)
    pairs = sorted(
        {(m.captures["lo"], m.captures["hi"]) for m in find_code_all(image, _CODE_PTRTAB)}
    )
    if len(pairs) < 2:
        raise _LayoutError(f"code-scan: found {len(pairs)} pointer-table access sites, need 2")
    chdp = _code_operands(image, _CODE_CHORDPTR, "chdp")
    tmpp = _code_operands(image, _CODE_TEMPOPTR, "tmpp")

    swm_pos = data.find(SWM_MAGIC, base)
    swm_header: Optional[bytes] = None
    seq_hint = None
    chord_len = tempo_len = 0
    if 0 <= swm_pos and swm_pos + TUNE_HEADER_SIZE <= len(data):
        swm_header = data[swm_pos : swm_pos + TUNE_HEADER_SIZE]
        seq_amount = swm_header[SEQUENCE_AMOUNT_POS]
        seq_hint = (seq_amount - 1) // SID_CHANNELS + 1 if seq_amount > 0 else 1
        chord_len = swm_header[CHORD_LENGTH_POS]
        tempo_len = swm_header[TEMPO_LENGTH_POS]
    else:
        # Magic-less export: derive chord/tempo lengths from the code-located
        # table bases (chord/tempo are contiguous ahead of the subtune table).
        chdt = _code_operands(image, _CODE_CHORDTAB, "chdt")
        tmpt = _code_operands(image, _CODE_TEMPOTAB, "tmpt")
        subt = _code_operands(image, _CODE_SUBTUNES, "subt")
        if len(tmpt) == 1:
            tempo_base = tmpt[0] + 1
            if len(subt) == 1:
                tempo_len = max(0, subt[0] - _RESTEMP_SHIFT - tempo_base)
            if len(chdt) == 1:
                chord_len = max(0, tempo_base - chdt[0])
        elif len(chdt) == 1 and len(subt) == 1:
            chord_len = max(0, (subt[0] - _RESTEMP_SHIFT) - chdt[0])

    ordered = sorted(pairs, key=lambda pair: pair[1], reverse=True)
    last: Optional[_LayoutError] = None
    for reloc in (0, load - _ORIG_LOAD):
        for pat_pair in ordered:
            for ins_pair in ordered:
                if ins_pair[0] >= pat_pair[0]:
                    continue
                try:
                    return _try_codescan_layout(
                        image,
                        byte,
                        chunk,
                        load,
                        end,
                        ins_pair,
                        pat_pair,
                        seq_hint,
                        chord_len,
                        tempo_len,
                        swm_header,
                        chdp,
                        tmpp,
                        reloc,
                    )
                except _LayoutError as exc:
                    last = exc
    raise last or _LayoutError("code-scan: no coherent layout")
