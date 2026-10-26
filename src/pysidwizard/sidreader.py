"""Read SID-Wizard-authored PSID (``.sid``) files.

Read-only, lossy companion to :mod:`pysidwizard.reader`: where ``parse_swm``
reads the editor workfile (``.swm``), :func:`parse_sid` recovers a playable
:class:`~pysidwizard.model.SWMFile` from the runtime image the exporter
bakes into a ``.sid``.

Usage and scope
---------------
Lossy on purpose: instrument names are discarded and per-pattern row-lengths
are best-effort, so do **not** round-trip the result through the writer.

Covers ``PSID``/``RSID`` tunes, plain and packed (``SWP``), single- and
multi-SID (:func:`~pysidwizard.sidfile.sid_format`), including multi-subtune
tunes (via :meth:`SWMFile.subtune`). Exports whose tables only materialise
under the player's init are recovered by running it (:func:`_player_layouts`).

Layout model
------------
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

Structural counts and, unlike the plain path, the chord/tempo-table lengths and
the tune's author all come from the ``SWP1`` block, not the ``SWM1`` header:
some packed exports ship a stale player-template ``SWM1`` header (placeholder
counts, author ``"SIW-WIZARD"``) or omit it entirely. The counts derive from
the table extents (e.g. ``patterns = ptn_hi_off - ptn_lo_off``), the chord table
runs from its base to the tempo-table base, the tempo table down to the
8-byte RESTEMP area below the subtunes, and the author sits at
``SWP1+AUTHOR_POS``. An ``SWM1`` header, when present, still supplies the
remaining lossy metadata (frame speed, driver/tuning); else model defaults.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from functools import lru_cache, wraps
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple, Union

from pysidtracker import (
    MEM_SIZE,
    PSID_MAGIC,
    RSID_MAGIC,
    BaseSidParser,
    CodePattern,
    Located,
    SidImage,
    SidParseError,
    Strategy,
    find_code_all,
    locate,
    run_init,
    wire_mpu,
)
from pysidtracker.facets import Source, inferred
from pysidtracker.locate import STATIC, VARIANT_FACET
from pysidtracker.oracle import DRIVER_VOLUME, SID_VOLUME
from pysidtracker.poweron import power_on_ram
from pysidtracker.registers import sid_model_for_flags
from pysidtracker.sidid import identify

from .constants import (
    AUTHOR_LEN,
    AUTHOR_POS,
    INST_FILTER_TABLE_PTR_POS,
    INST_HEADER_SIZE,
    PACKED_MAX,
    PACKED_MIN,
    SWM1_FORMAT,
    SWM_MAGIC,
    TABLE_END,
    SWMFormat,
    subtunes_for,
)
from .errors import SIDFormatError, SWMFormatError
from .features import detect_variant, features_for_driver
from .model import (
    End,
    Instrument,
    Pattern,
    PlayPattern,
    Row,
    SWMFile,
    decode_sequence,
    unpack_pattern,
)
from .player import FREQ_TABLE_SPAN, FREQ_TABLE_TAIL_LEN, NOTE_FREQ_LO
from .reader import LATEST_PLAYER_VERSION, apply_player_image
from .sidfile import (
    as_container,
    find_player_signature,
    parse_psid_header,
    sid_chips,
    sid_format,
)
from .variant import PlayerImage

# SID-Wizard's "packed" (``SWP``) export keeps an ``SWP1`` magic (the ``SWM1``
# header with its ``M`` turned into ``P``). Despite the name the tune data is
# not compressed: it is the same runtime layout with relative pointers located
# via an offset-table in the ``SWP1`` header (see the module docstring and
# :func:`_swp_layout`).
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

# The runtime "subtunes" table's block size and the RESTEMP gap ahead of the tempo
# programs scale with the SID count: :attr:`SWMFormat.subtune_block` /
# :attr:`SWMFormat.restemp_shift`.
# How far below inst_lo to scan for the subtune table (covers the chord- and
# tempo-pointer tables, whose combined size is bounded by the editor limits).
_SUBTUNE_SCAN_WINDOW = 320
# Per-pattern / per-sequence walk guards (defensive bounds).
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
# :func:`_swm_layouts` cannot resolve. Idioms are mapped from the vendored SID-Wizard
# player source (``native/sources/include/player.asm``): ``p_ptnl1``/``p_ptnh1``
# and ``p_insl3``/``p_insh3`` (``ldy CURx,x; lda PTRLO,y; sta zp; lda PTRHI,y;
# sta zp``), ``p_subt1`` (``adc #imm; tay; lda SUBTUNES,y``, or V1.0's ``asl; tay;
# ldx #1; lda SUBTUNES,y``), ``p_chdt1``
# (``lda CHORDS,y; cmp #$7E``), ``p_chdp1``/``p_tmpp1`` (``tay; lda TBL,y``),
# ``p_tmpt1`` (``sec; sbc TEMPOTBL-1,y``). The magic-less player-code signature
# that anchors detection lives in :mod:`pysidwizard.sidfile`.
# Pattern-LO/HI and instrument-LO/HI pointer tables share this skeleton; the two
# real matches are told apart by address order (the instrument tables sit below
# the pattern tables) and validated by decoding.
_CODE_PTRTAB = CodePattern("BC ?? ?? B9 {lo:w} 85 ?? B9 {hi:w} 85 ??")
# ``p_seqtN lda SEQUENCES+n*seqbound,y ; rts`` -- the per-channel orderlist
# reader in the RAM (non-subtune-indirect) player. Each operand is self-modified
# at init to that channel's packed sequence base (:func:`_player_layouts`).
_CODE_SEQ_DIRECT = CodePattern("B9 {seq:w} 60")
_CODE_SUBTUNES = CodePattern("69 ?? A8 B9 {subt:w}")
# V1.0's SETSTUNE (1.0 player.asm): ``asl ; tay ; ldx #1 ; p_subt1 lda SUBTUNES,y``.
_CODE_SUBTUNES_V10 = CodePattern("0A A8 A2 01 B9 {subt:w}")
_CODE_CHORDTAB = CodePattern("BC ?? ?? B9 {chdt:w} C9 7E D0")
_CODE_CHORDPTR = CodePattern("A8 B9 {chdp:w} 9D")
_CODE_TEMPOPTR = CodePattern("A8 B9 {tmpp:w} 4C")
_CODE_TEMPOTAB = CodePattern("38 F9 {tmpt:w} F0")
# A partially-relocated export leaves some pointer-table entries at their
# original (pre-relocation) address; the relocation delta is read from the player
# code, where two operands reference the same table (:func:`_reloc_delta_candidates`),
# never from a guessed origin. Any export the code-derived delta can't repair
# falls through to :func:`_player_layouts`, which runs the tune's own init.


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


def _freq_table_tail(data: bytes) -> bytes:
    """Image bytes following the player's FREQTBL, anchored on the table itself.

    ``and #$7F`` / ``ldy DPITCH,x`` pitch loads (player.asm:2523-2535) index past
    the table's 96 notes into whatever the assembler put there, so reproducing
    those frequencies needs the real bytes rather than a clamp.
    """
    pos = data.find(NOTE_FREQ_LO)
    if pos < 0:
        return b""
    end = pos + len(NOTE_FREQ_LO)
    return data[end : end + FREQ_TABLE_TAIL_LEN]


# ABSPTCH's pitch load (player.asm:2531-2535): ``tay ; lda FREQTBL,y ; sta FREQLO,x ; lda FREQTBH,y ; sta FREQHI,x``.
_CODE_FREQTBL = CodePattern("A8 B9 {lo:w} 9D ?? ?? B9 {hi:w} 9D")
# FREQTBL follows FREQTBH directly, or after the 8-byte keyboard-track slope.
_FREQTBH_SPANS = (len(NOTE_FREQ_LO), len(NOTE_FREQ_LO) + 8)


def _freq_table_images(data: bytes) -> tuple:
    """``(lo, hi)``: the 256-byte spans from the image's FREQTBL / FREQTBH bases.

    Read from the player's own pitch load, so an NTSC export's table and the
    bytes past each table are the ones its player indexes. Empty when no load
    matches.
    """
    image = SidImage.from_sid(data)
    for match in find_code_all(image, _CODE_FREQTBL):
        lo, hi = match.captures["lo"], match.captures["hi"]
        if lo - hi in _FREQTBH_SPANS:
            return bytes(image.mem[lo : lo + FREQ_TABLE_SPAN]), bytes(
                image.mem[hi : hi + FREQ_TABLE_SPAN]
            )
    return b"", b""


# COMMONREGS (player.asm:748-797): ``lda #FSWITCH ; ora #RESONIB ; sta $D417 ; ... ; clc`` then, past the
# optional FILTKBTRACK prelude, CTFHGHO's ``lda|adc #``; CTFLGHO's ``lda # ; sta $D415``
# follows ``sta $D416``.
_FILTER_HI = re.compile(
    rb"\xa9(.)\x09(.)\x8d\x17\xd4\xa9.\x09.\x8d\x18\xd4\x18"
    rb"(?:\xa9.\xf0.\xae..(?:\x7d..|\x75.)\xa8\xb9..\x69|\xa9)(.)",
    re.S,
)
_FILTER_LO = re.compile(rb"\x8d\x16\xd4(?:\x8d..)?\xa9(.)\x8d\x15\xd4", re.S)
_STA_FCUT_HI = b"\x8d\x16\xd4"


def _filter_init(data: bytes) -> tuple:
    """``(resonance, cutoff_hi, cutoff_lo, switch)``: the filter operands the first play call writes.

    Zero for a component the image has no code for (no FINEFILTSWEEP, say).
    """
    hi = _FILTER_HI.search(data)
    if hi is None:
        return (0, 0, 0, 0)
    pos = data.find(_STA_FCUT_HI, hi.end())
    lo = _FILTER_LO.match(data, pos) if pos >= 0 else None
    return (hi.group(2)[0], hi.group(3)[0], lo.group(1)[0] if lo else 0, hi.group(1)[0])


# A PLAYER entry gated on a self-modifying toggle: ``lda #s ; eor #m ; sta <lda operand> ;
# bpl|bmi +1 ; rts`` before the player body, so the body runs only on calls whose toggled
# operand has bit 7 clear (``bpl``) or set (``bmi``).
_PLAY_GATE = re.compile(rb"\xa9(.)\x49(.)\x8d(..)([\x10\x30])\x01\x60", re.S)
_BPL = 0x10
_SIGN = 0x80


def _play_gate(mem, base: int) -> tuple:
    """``(operand, mask, play_sign)`` of the gate at ``base``'s ``playsub`` target, else ``()``."""
    mem = bytes(mem)
    playsub = base + _JMP_LEN
    if mem[playsub : playsub + 1] != bytes([_JMP]):
        return ()
    target = int.from_bytes(mem[playsub + 1 : playsub + _JMP_LEN], "little")
    match = _PLAY_GATE.match(mem, target)
    if match is None or int.from_bytes(match.group(3), "little") != target + 1:
        return ()
    return (match.group(1)[0], match.group(2)[0], 0 if match.group(4)[0] == _BPL else _SIGN)


def _player_code(data: bytes) -> tuple:
    """``(code, load)`` of the player the ``.sid``'s init enters.

    With a single embedded player (or an init entering none) the whole image is read.
    """
    header = parse_psid_header(data)
    code, load = data[header.data_start :], header.real_load_address
    entry = (header.init_address or load) - load
    bases = _player_bases(code)
    if len(bases) > 1 and entry in bases:
        following = [b for b in bases if b > entry]
        code, load = code[entry : following[0] if following else None], load + entry
    return code, load


def parse_sid(data: bytes) -> SWMFile:
    """Parse a SID-Wizard ``.sid`` image into a :class:`SWMFile`.

    Its :attr:`~pysidwizard.model.SWMFile.player` holds the image's build flags
    (:func:`~pysidwizard.features.detect_variant`), release quirks and tables,
    so the player reproduces its code and past-table pitch lookups exactly.
    """
    data = as_container(data)
    swm = _parse_sid_layout(data)
    code, load = _player_code(data)
    image = SidImage.from_sid(data)
    base = swm.player or PlayerImage(features_for_driver(swm.driver_type))
    freq_lo, freq_hi = _freq_table_images(data)
    features, quirks, provenance = detect_variant(code, load, data, base.features)
    swm.player = replace(
        base,
        features=features,
        quirks=quirks,
        provenance={**base.provenance, **provenance},
        freq_lo=freq_lo,
        freq_hi=freq_hi,
        freq_table_tail=_freq_table_tail(data),
        filter_init=_filter_init(data),
        play_gate=_play_gate(image.mem, image.header.init_address or image.load),
    )
    return swm


def _sid_header(data: bytes):
    """The container header, or :class:`SIDFormatError` for a non-``PSID`` / ``RSID`` magic."""
    header = parse_psid_header(data)
    if header.magic not in (PSID_MAGIC, RSID_MAGIC):
        raise SIDFormatError(f"unsupported SID magic {header.magic!r}")
    return header


def _parse_sid_layout(data: bytes) -> SWMFile:
    """Resolve a ``.sid`` image's SID-Wizard tune tables into a :class:`SWMFile`.

    The returned model plays under :class:`~pysidwizard.player.SWMPlayer`
    exactly as a parsed ``.swm`` would (multi-subtune tunes expose their other
    subtunes via :meth:`SWMFile.subtune`). Read-only and lossy (see the module
    docstring).

    Both ``PSID`` and ``RSID`` containers are accepted: RSID uses a different
    init/play convention but the embedded SID-Wizard tune-data layout is
    identical, so any RSID whose tables resolve is parsed too. Plain and
    *packed* (``SWP``) exports are both decoded, single- and multi-SID alike
    (:func:`sid_format`: the runtime tables group ``3 * sid_amount`` orderlists
    per subtune). Raises :class:`SIDFormatError` for: an unrecognised magic, a
    missing tune header on a plain export, truncation, or pointer tables that do
    not resolve.
    """
    header = _sid_header(data)
    fmt = sid_format(data, header)
    if fmt.sid_amount < sid_chips(header):
        raise SIDFormatError(
            f"the container drives {sid_chips(header)} SIDs but its first module is "
            f"{fmt.sid_amount}-SID ({fmt.magic.decode()!r}): one player per chip is not one module"
        )
    load = header.real_load_address
    base = header.data_start  # file offset that maps to ``load``
    swp_pos = data.find(SWP_MAGIC, base)
    swm_pos = data.find(fmt.magic, base)
    if swp_pos < 0 and swm_pos >= 0 and swm_pos + fmt.header_size > len(data):
        raise SIDFormatError(f"{fmt.magic.decode()!r} tune header is truncated")
    image = SidImage.from_sid(data)
    codescan = _codescan_strategy(data, load, base, fmt)
    player = _player_strategy(data, load, base, fmt)
    if swp_pos >= 0:
        # A "packed" (SWP) export: relative-pointer tables located via the SWP1 offset-table. Not
        # compressed; decoded directly.
        located = _locate_swm(image, [_swp_strategy(data, load, base, swp_pos, fmt)])
        if not located.ok:
            reason = _last_rejection(located, "swp")
            raise SIDFormatError(f"SID-Wizard packed (SWP) tables did not resolve: {reason}")
    elif swm_pos < 0:
        # No ``SWM1`` / ``SWP1`` magic: a magic-less (stripped / relocated) export. Recover it by
        # reading the table bases straight from the player-code operands, anchored on the SID-Wizard
        # 1.x player signature; if the static image alone doesn't resolve, run the player's init.
        located = _locate_swm(image, [codescan, player])
        if not located.ok:
            reason = _last_rejection(located, "via-player")
            raise SIDFormatError(
                "no 'SWM1' tune header found and the SID-Wizard player tables "
                f"could not be located from the player code: {reason}"
            )
    else:
        # The header end-probe assumes an in-place absolute-pointer layout. When it fails the export
        # is relocated / alternate-layout: fall back to the player-code operands (relocation-invariant,
        # which also repairs partially-relocated pointer tables), then to the tune's own player (the
        # static image holds no coherent layout: stale/template header, or tune data only
        # materialised by the player's init/relocation).
        swm = _swm_strategy(data, load, base, data[swm_pos : swm_pos + fmt.header_size], fmt)
        located = _locate_swm(image, [swm, codescan, player])
        if not located.ok:
            raise SIDFormatError(
                f"{fmt.magic.decode()!r} tune header found but its pointer tables did not resolve to a "
                "coherent layout (stale/template header or unmaterialised relocated "
                f"export); out of the direct-load scope. Last probe: "
                f"{_last_rejection(located, 'swm-header')}"
            )
    swm = located.layout
    _record_layout(swm, located.strategy, located.variant.source)
    return swm


# The player jump table (player.asm:168) starts ``inisub jmp INITER ; playsub jmp
# PLAYER`` and is followed by the tracker-info text.
_JMP = 0x4C
_JMP_LEN = 3
_PLAYER_MARK = b"SID-WIZARD"
_MARK_REACH = 0x30
# Init budget for reaching INITER; generous for wrappers that unpack first.
_INIT_MAX_CYCLES = 8_000_000
# PSID header fields (big-endian) the module window rewrites.
_PSID_LOAD = slice(0x08, 0x0A)
_PSID_INIT = slice(0x0A, 0x0C)
_PSID_PLAY = slice(0x0C, 0x0E)
# The SID's I/O page: 32-byte register mirrors, 25 registers each.
SID_IO, SID_IO_END = 0xD400, 0xD800
SID_MIRROR_MASK = 0x1F
SID_REGS = 25
# SETSEQA (player.asm:2669) stores the subtune times the SUBTUNES block size in SUBTPOS.


def _player_bases(mem) -> List[int]:
    """Jump-table addresses of the SID-Wizard players in ``mem``, ascending.

    A base is the lowest ``jmp ; jmp`` pair within :data:`_MARK_REACH` bytes
    below a ``SID-WIZARD`` tracker-info text whose first ``jmp`` (INITER) lands
    past that text, so a wrapper's own jump table below the player is skipped.
    """
    buf = bytes(mem)
    bases = []
    mark = buf.find(_PLAYER_MARK)
    while mark >= 0:
        base = next(
            (
                a
                for a in range(max(0, mark - _MARK_REACH), mark)
                if buf[a] == _JMP and buf[a + _JMP_LEN] == _JMP and _word(buf, a + 1) >= mark
            ),
            None,
        )
        if base is not None:
            bases.append(base)
        mark = buf.find(_PLAYER_MARK, mark + 1)
    return sorted(set(bases))


def _word(buf, addr: int) -> int:
    return buf[addr] | (buf[addr + 1] << 8)


def _wrapped_bases(header, mem) -> List[int]:
    """The player base a wrapper init calls when no ``SID-WIZARD`` text marks it: the jump
    table the header's play address enters (``playsub``), unless init is that table."""
    play = header.play_address
    if play < _JMP_LEN or header.init_address == play - _JMP_LEN:
        return []
    return [play - _JMP_LEN] if mem[play - _JMP_LEN] == mem[play] == _JMP else []


def _play_target(mem, play: int) -> int:
    """``play`` followed through ``jmp``s (a wrapper's init may patch them)."""
    seen = set()
    while mem[play] == _JMP and play not in seen:
        seen.add(play)
        play = _word(mem, play + 1)
    return play


def _song_entry(data: bytes, song: int):
    """``(entry, sid, mem)``: the player song ``song``'s init starts, and the SID and memory it leaves.

    Runs the tune's init (accumulator = 0-based ``song``). ``base`` is the last
    player jump table it enters and ``mem`` a snapshot of memory at that entry
    (the wrapper's own data patches applied, INITER not yet run). ``subtune`` is
    what that player's SETSEQA stores (``None`` when it never runs during init)
    and ``swp`` the SWP data base INITER receives in X / Y. ``None`` when the init
    enters no player. ``sid`` is the register file the whole init wrote (the
    chip's ``$20``-byte mirrors folded) over the PSID driver's maximum ``$D418``
    volume, which the first play call starts from,
    and ``mem`` the memory the whole init left.
    """
    image = SidImage.from_sid(data)
    header = image.header
    bases = _player_bases(image.mem) or _wrapped_bases(header, image.mem)
    bounds = dict(zip(bases, bases[1:] + [MEM_SIZE], strict=False))
    subtpos = {
        m.addr + 1: base
        for base in bases
        for m in find_code_all(image.mem, _CODE_SUBTUNES, start=base, end=bounds[base])
    }
    mpu, mem = wire_mpu(image.mem, sid_model=sid_model_for_flags(header.flags))
    written = {}
    mem.subscribe_to_write(subtpos, lambda addr, value: written.__setitem__(subtpos[addr], value))
    sid = bytearray(SID_REGS)
    sid[SID_VOLUME - SID_IO] = DRIVER_VOLUME

    def _sid_write(addr: int, value: int) -> None:
        reg = addr & SID_MIRROR_MASK
        if reg < SID_REGS:
            sid[reg] = value

    mem.subscribe_to_write(range(SID_IO, SID_IO_END), _sid_write)
    entry = None
    top = mpu.sp
    for byte_ in (0x00, 0x01):  # return address the final RTS climbs above ``top``
        mem[0x0100 + mpu.sp] = byte_
        mpu.sp = (mpu.sp - 1) & 0xFF
    mpu.a = song & 0xFF
    mpu.pc = header.init_address or header.real_load_address
    start = mpu.processorCycles
    while mpu.sp < top and mpu.processorCycles - start < _INIT_MAX_CYCLES:
        if mpu.pc in bounds:
            entry = (bytes(image.mem), mpu.pc, (mpu.y << 8) | mpu.x)
        mpu.step()
    if entry is None:
        target = _play_target(image.mem, header.play_address) if header.play_address else None
        if target is not None:
            stop = min([b for b in bases if b > target] + [image.end])
            foreign = identify(bytes(image.mem[target:stop]))
            if foreign and "SidWizard" not in foreign:
                raise SIDFormatError(
                    f"song {song + 1}'s init enters no SID-Wizard player "
                    f"({foreign} at ${target:04X})"
                )
        return None, bytes(sid), bytes(image.mem)
    snapshot, base, swp = entry
    value = written.get(base)
    has_subtunes = any(owner == base for owner in subtpos.values())
    block = sid_format(data, header).subtune_block
    subtune = value // block if value is not None else (None if has_subtunes else 0)
    return (snapshot, base, subtune, swp), bytes(sid), bytes(image.mem)


def _module_window(data: bytes, mem: bytes, base: int, swp: int) -> bytes:
    """A single-player ``.sid`` of the module the player at ``base`` plays.

    The window is ``mem`` from the module start up to the next player jump
    table, with init / play pointed at ``base``. A packed player's module starts
    at its SWP data base or its jump table, whichever is lower, so the window
    holds the player code the reader takes build flags and tables from; a plain
    player's at its jump table.
    """
    header = parse_psid_header(data)
    start = min(base, swp) if mem[swp : swp + len(SWP_MAGIC)] == SWP_MAGIC else base
    following = [b for b in _player_bases(mem) if b > max(start, base)]
    stop = following[0] if following else header.real_load_address + len(data) - header.data_start
    out = bytearray(data[: header.data_offset])
    out[_PSID_LOAD] = bytes(2)
    out[_PSID_INIT] = base.to_bytes(2, "big")
    out[_PSID_PLAY] = (base + _JMP_LEN).to_bytes(2, "big")
    return bytes(out) + start.to_bytes(2, "little") + mem[start:stop]


def parse_sid_song(data: bytes, song: int) -> SWMFile:
    """The single-subtune module that 1-based header ``song`` plays.

    The song number is only the accumulator the tune's init receives; a wrapper
    may map it to another subtune, patch the tune data, or call one of several
    embedded players (each with its own module). This runs the init up to the
    SID-Wizard player it enters and decodes that player's module from the memory
    the wrapper prepared, selecting the subtune INITER receives in A
    (``SETSTUNE``, player.asm:2611). An init that enters the player directly is
    the plain :func:`parse_sid` of the file. Raises :class:`SIDFormatError` when
    the selected subtune is not in the decoded module.
    """
    data = as_container(data)
    header = _sid_header(data)
    found, sid, after = _song_entry(data, song - 1)
    if found is None or found[2] is None:
        swm, subtune = parse_sid(data), song - 1
    else:
        mem, base, subtune, swp = found
        if base == (header.init_address or header.real_load_address):
            swm = parse_sid(data)
            _patched_instruments(swm, data, after, base, swp)
        else:
            swm = parse_sid(_module_window(data, mem, base, swp))
    if not 0 <= subtune < swm.subtune_count:
        raise SIDFormatError(
            f"song {song} selects subtune {subtune}, but the module decodes "
            f"{swm.subtune_count} subtune(s)"
        )
    zp = after[_PLAYERZP] | (after[_PLAYERZP + 1] << 8)
    init = {
        "sid_init": sid,
        # INITER zeroes FSWITCH only under FILT_CTRL_FX_ON (player.asm:418), so read the operands it left.
        "filter_init": _filter_init(after[found[1] if found else 0 :]),
        "zp_init": (after + after)[zp : zp + _ZP_REACH],
    }
    if found is not None:
        init["play_gate"] = _play_gate(after, found[1])
    swm.player = replace(swm.player, **init)
    return swm.subtune(subtune)


def _patched_instruments(swm: SWMFile, data: bytes, after: bytes, base: int, swp: int) -> None:
    """Take ``swm``'s instruments as the whole init left them in memory.

    A wrapper may patch instrument bytes per song before INITER (a table of
    address / per-song value rows), and PLAYERZP reads the patched memory. Only
    the instruments are taken: the init also rewrites the SWM header region.
    """
    try:
        runtime = parse_sid(_module_window(data, after, base, swp))
    except SIDFormatError:
        return
    if len(runtime.instruments) == len(swm.instruments):
        swm.instruments = runtime.instruments
        swm.player = replace(
            swm.player,
            dummy_instrument=runtime.player.dummy_instrument,
            spare_instruments=runtime.player.spare_instruments,
        )


def read_sid(path: Union[str, os.PathLike[str]]) -> SWMFile:
    """Read a SID-Wizard ``.sid`` file from ``path`` and return a :class:`SWMFile`."""
    with open(path, "rb") as fh:
        return parse_sid(fh.read())


def parse_swp(data: bytes, player_version: str | None = LATEST_PLAYER_VERSION) -> SWMFile:
    """Decode a standalone SID-Maker-SWP (``SWP1``) packed tune to :class:`SWMFile`.

    ``SWP1`` magic sits at offset 0 or after a 2-byte PRG load prefix; with no
    ``SWM1`` header the lengths and author come from the ``SWP1`` block. The
    ``player_version`` picks the bare module's FREQTBL image as in parse_swm.
    """
    if len(data) >= 4 and data[0:4] == SWP_MAGIC:
        magic, load = 0, 0
    elif len(data) >= 6 and data[2:6] == SWP_MAGIC:
        magic, load = 2, int.from_bytes(data[0:2], "little")
    else:
        raise SIDFormatError("input is not an SWP module (no 'SWP1' magic at offset 0 or 2)")
    try:
        byte, chunk = _memory_accessors(data, load, 0)
        swm = _decode_layout(_swp_layout(data, load, 0, magic, SWM1_FORMAT), _Reader(byte, chunk))
    except _LayoutError as exc:
        raise SIDFormatError(f"SWP tables did not resolve: {exc}") from exc
    _record_layout(swm, "swp", inferred("layout strategy static"))
    return apply_player_image(swm, player_version)


def read_swp(
    path: Union[str, os.PathLike[str]], player_version: str | None = LATEST_PLAYER_VERSION
) -> SWMFile:
    """Read a standalone SID-Wizard ``.swp`` file from ``path`` into a :class:`SWMFile`."""
    with open(path, "rb") as fh:
        return parse_swp(fh.read(), player_version)


class SidWizardSidParser(BaseSidParser):
    """:class:`pysidtracker.BaseSidParser` adapter for SID-Wizard ``.sid`` tunes.

    Gives SID-Wizard the shared ``read``/``parse``/``detect`` surface: parsing
    delegates to :func:`parse_sid` for container tunes and :func:`parse_swp` for
    standalone packed (``SWP1``) tunes, and :meth:`recognize` anchors on the
    ``SWM1`` tune header, the ``SWP1`` packed-export magic, or — for magic-less
    exports — the SID-Wizard 1.x player-code signature, so
    :meth:`~pysidtracker.BaseSidParser.detect` classifies a direct-load tune as
    :attr:`~pysidtracker.PlayroutineKind.DIRECT`.
    """

    error_class = SIDFormatError

    def parse(self, data: bytes, subtune: Optional[int] = None, **kwargs: Any) -> SWMFile:
        """The tune's module, or with ``subtune`` the single-subtune module 0-based ``subtune``
        plays (the container's song ``subtune + 1``, :func:`parse_sid_song`)."""
        # A standalone packed tune (SWP1 at offset 0 or after a 2-byte load prefix) has no container.
        if data[:4] == SWP_MAGIC or (len(data) >= 6 and data[2:6] == SWP_MAGIC):
            swm = parse_swp(data, **kwargs)
            return swm if subtune is None else swm.subtune(subtune)
        if subtune is None:
            return parse_sid(data, **kwargs)
        return parse_sid_song(data, subtune + 1)

    def recognize(self, image: SidImage) -> object:
        pos = image.find(SWM_MAGIC)
        if pos < 0:
            pos = image.find(SWP_MAGIC)
        if pos >= 0:
            return pos
        return find_player_signature(image)


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


def _filter_table_end(byte, start: int, bound: int) -> Optional[int]:
    """Address of a filter table's terminating ``$FF`` row (column 0), else None.

    Filter rows are 3 bytes; only column 0 is a command (player.asm
    ``FLTPOSI``/``NOCWEEP``: ``$FF`` end, ``$FE`` jump, ``$80..$FD`` set,
    ``$00..$7F`` sweep). ``$FF`` in the sweep-amount or keyboard-tracking
    columns is data, so the walk steps whole rows.
    """
    cursor = start
    while cursor < bound:
        if byte(cursor) == TABLE_END:
            return cursor
        cursor += 3
    return None


def _decode_instruments(
    byte, chunk, inst_lo, inst_hi, inst_amount, load, end, ptr_base=0, reloc_delta=0
):
    """Decode the ``inst_amount`` instruments (1-based) and the image slots past them.

    Each instrument is a 16-byte header followed by three inline ``$FF``-
    terminated tables (waveform / pulse-width / filter). The body runs to the
    filter table's terminating ``$FF`` row (column 0) and is handed to
    :meth:`Instrument.decode`. Returns ``(instruments, slots)`` (see
    :func:`_decode_slots`).
    """
    instruments: List[Instrument] = []
    addrs = [
        _resolve_ptr(_slot_ptr(byte, inst_lo, inst_hi, k), ptr_base, load, end, reloc_delta)
        for k in range(MAX_INSTRUMENT_SLOT + 1)
    ]
    for k in range(1, inst_amount + 1):
        tries = (addrs[k], addrs[k] + reloc_delta) if reloc_delta else (addrs[k],)
        for n, addr in enumerate(tries, 1):
            try:
                instruments.append(_decode_slot(byte, chunk, addr, k, load, end, inst_lo))
                addrs[k] = addr
                break
            except _LayoutError:
                if n == len(tries):
                    raise
    return instruments, _decode_slots(
        byte, chunk, addrs, inst_amount, inst_lo, lambda a: load <= a < end
    )


def _decode_slot(byte, chunk, addr: int, k: int, load: int, end: int, inst_lo: int) -> Instrument:
    """Instrument ``k`` at ``addr``, or :class:`_LayoutError`."""
    if not load <= addr < end:
        raise _LayoutError(f"instrument {k} pointer {addr:#06x} out of range")
    cursor = _filter_table_end(byte, addr + byte(addr + INST_FILTER_TABLE_PTR_POS), inst_lo)
    if cursor is None:
        raise _LayoutError(f"instrument {k} filter table unterminated")
    try:
        return _decode_at(byte, chunk, addr, cursor, name=b"INST %02d" % k)
    except _LayoutError:
        raise
    except Exception as exc:  # SWMFormatError from a malformed body
        raise _LayoutError(f"instrument {k} did not decode: {exc}") from exc


# SEL_INS's ``cmp #maxinstamount`` bound: CURINS ranges over slots 1..$3E.
MAX_INSTRUMENT_SLOT = 0x3E


@lru_cache(maxsize=1)
def _power_on() -> bytes:
    return bytes(power_on_ram())


def _booted(byte):
    """``byte`` over the image, and libsidplayfp's power-on RAM everywhere else."""
    ram = _power_on()

    def read(addr: int) -> int:
        try:
            return byte(addr)
        except _LayoutError:
            return ram[addr & 0xFFFF]

    return read


def _slot_ptr(byte, inst_lo: int, inst_hi: int, k: int) -> int:
    """INSPTLO/HI slot ``k``; a slot past the tables reads whatever bytes follow them."""
    byte = _booted(byte) if k else byte
    return byte(inst_lo + k) | (byte(inst_hi + k) << 8)


def _decode_slots(byte, chunk, addrs, amount, bound, inrange) -> Dict[int, Instrument]:
    """The instruments INSPTLO/HI slot 0 and the slots past ``amount`` point at.

    PLAYERZP reads slot 0 whenever a path loads CURINS=0 without testing it.
    The exporter links it to instrument 1 (packdepack.inc:621-633), but a
    re-packed image can point it elsewhere, e.g. at a blank instrument while
    instrument 1's own pointer is left stale; slot 0 is left out when it is
    instrument 1's pointer or does not hold a decodable instrument. A slot past
    the count (SEL_INS takes any reference below $3F) reads the pointer bytes
    that follow the tables, so it is decoded leniently from wherever it lands.
    """
    slots: Dict[int, Instrument] = {}
    addr = addrs[0]
    if addr not in addrs[1:2] and inrange(addr):
        try:
            cursor = _filter_table_end(byte, addr + byte(addr + INST_FILTER_TABLE_PTR_POS), bound)
            if cursor is not None:
                slots[0] = _decode_at(byte, chunk, addr, cursor, name=b"INST 00")
        except SWMFormatError:
            pass
    booted = _booted(byte)
    for k in range(amount + 1, len(addrs)):
        ram = bytes(booted((addrs[k] + y) & 0xFFFF) for y in range(_ZP_REACH))
        start = max(ram[INST_FILTER_TABLE_PTR_POS], INST_HEADER_SIZE + 1)
        end = _filter_table_end(ram.__getitem__, start, _SPARE_BODY)
        body = ram[: _SPARE_BODY if end is None else end]
        slots[k] = Instrument.decode(body, name=b"INST %02d" % k, strict=False)
        slots[k].ram = ram
    return slots


# PLAYERZP's ``lda (PLAYERZP),y`` reach, and its zeropage address (player.asm:7).
_ZP_REACH = 0x100
_PLAYERZP = 0xFE
# The longest body whose re-encoded table pointers still fit a byte.
_SPARE_BODY = _ZP_REACH - 3


def _decode_at(byte, chunk, addr: int, cursor: int, name: bytes) -> Instrument:
    """The instrument whose body is ``[addr, cursor)``, with its :attr:`~pysidwizard.model.Instrument.ram` window."""
    ins = Instrument.decode(chunk(addr, cursor - addr), name=name)
    ins.ram = _ram_window(byte, addr)
    return ins


def _ram_window(byte, addr: int) -> bytes:
    """The bytes ``lda (PLAYERZP),y`` can reach from ``addr``, up to the image's end."""
    ram = bytearray()
    for a in range(addr, addr + _ZP_REACH):
        try:
            ram.append(byte(a))
        except _LayoutError:
            break
    return bytes(ram)


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
        pat = Pattern.decode(unpacked, length=length)
        pat.ram = _ram_window(byte, addr)
        patterns.append(pat)
    return patterns, min_pattern


def _locate_subtune_table(byte, inst_lo, subtunes, seq_amount, load, min_pattern, fmt):
    """Find the subtune-table base just below ``inst_lo``.

    The chord- and tempo-pointer tables sit between the subtune table and
    ``inst_lo`` and their sizes are not in the header, so scan downward for the
    highest block whose ``seq_amount`` sequence pointers all land in the
    sequences region (i.e. below the lowest pattern). The chord/tempo pointer
    tables point *into* the high chord/tempo tables, so they never satisfy this
    test — making the first match the genuine subtune table.
    """
    top = inst_lo - fmt.subtune_block * subtunes
    floor = max(load, top - _SUBTUNE_SCAN_WINDOW)
    for candidate in range(top, floor - 1, -1):
        if _subtune_pointers_valid(byte, candidate, seq_amount, load, min_pattern, fmt):
            return candidate
    raise _LayoutError("could not locate the subtune table")


def _subtune_block_addr(base: int, seq_index: int, fmt: SWMFormat) -> int:
    subtune, channel = divmod(seq_index, fmt.channels)
    return base + fmt.subtune_block * subtune + 2 * channel


def _subtune_pointers_valid(
    byte, base, seq_amount, load, min_pattern, fmt, end=None, reloc_delta=0
) -> bool:
    scan_end = end if end is not None else min_pattern
    for k in range(seq_amount):
        blk = _subtune_block_addr(base, k, fmt)
        try:
            raw = byte(blk) | (byte(blk + 1) << 8)
        except _LayoutError:
            return False
        ptr = _resolve_ptr(raw, 0, load, scan_end, reloc_delta)
        if not (load <= ptr < min_pattern):
            return False
    return True


def _decode_sequences(
    byte, chunk, subtune_base, seq_amount, load, end, fmt, ptr_base=0, reloc_delta=0
):
    """Decode the ``seq_amount`` channel sequences via the subtune table.

    Sequence ``k`` lives in subtune ``k // channels`` channel ``k % channels``; each runs
    from its pointer to the ``$FE`` (end) or ``$FF`` + loop-position-byte
    terminator, matching the runtime player.
    """
    sequences: List[List] = []
    for k in range(seq_amount):
        blk = _subtune_block_addr(subtune_base, k, fmt)
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


def _subtune_operands(image: SidImage) -> list:
    """The ``SUBTUNES`` table bases ``p_subt1`` reads, in either SETSTUNE form."""
    return sorted(
        set(_code_operands(image, _CODE_SUBTUNES, "subt"))
        | set(_code_operands(image, _CODE_SUBTUNES_V10, "subt"))
    )


def _iter_subtune_bases(
    byte, image, inst_lo, min_pattern, load, end, seq_hint, chdp, tmpp, reloc, fmt
):
    """Yield ``(subtune_base, subtunes)`` candidates for the sequence-pointer table.

    A code-located base (``p_subt1``) is offered first; then every position in
    the scan window below ``inst_lo`` whose sequence pointers land in the
    sequences region. The caller decodes each and keeps the first whose
    orderlist pattern references all resolve, so a false-positive block is
    skipped rather than mis-parsed.
    """
    seen = set()
    for sb in _subtune_operands(image):
        uppers = [x for x in ([inst_lo] + chdp + tmpp) if x > sb]
        if not uppers:
            continue
        n = seq_hint or ((min(uppers) - sb) // fmt.subtune_block)
        if n >= 1 and (sb, n) not in seen:
            seen.add((sb, n))
            yield sb, n
    counts = [seq_hint] if seq_hint else list(range(8, 0, -1))
    for n in counts:
        if not n or n < 1:
            continue
        seq_amount = n * fmt.channels
        top = inst_lo - fmt.subtune_block * n
        floor = max(load, top - _SUBTUNE_SCAN_WINDOW)
        for sb in range(top, floor - 1, -1):
            if (sb, n) in seen:
                continue
            if _subtune_pointers_valid(byte, sb, seq_amount, load, min_pattern, fmt, end, reloc):
                seen.add((sb, n))
                yield sb, n


def _codescan_chord_tempo(image, chunk, subtune_base, min_pattern, chord_len, tempo_len, fmt):
    """Return ``(chord_table, tempo_table)`` bytes for a code-scanned layout.

    Prefers the code-located table bases (``p_chdt1`` / ``p_tmpt1``); falls back
    to the geometry below the subtune table (as :func:`_chord_tempo` does) for either
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
        chord_base = subtune_base - fmt.restemp_shift - tempo_len - chord_len
        if chord_base > min_pattern:
            if chord_len > 0 and not chord_table:
                try:
                    chord_table = chunk(chord_base, chord_len)
                except _LayoutError:
                    pass
            if tempo_len > 0 and not tempo_table:
                try:
                    tempo_table = chunk(chord_base + chord_len + fmt.restemp_shift, tempo_len)
                except _LayoutError:
                    pass
    return chord_table, tempo_table


# SID-Maker builds the player and tune data at PLAYERADDR (settings.cfg); a relocator that misses
# pointer-table entries leaves them at their PLAYERADDR-based address.
PLAYERADDR = 0x1000


def _reloc_delta_candidates(pairs, load, end):
    """Relocation deltas read straight from the player code.

    A partially-relocated export references the same pointer table from two
    code sites, one operand relocated (inside the loaded image) and one still
    holding the pre-relocation address. Their difference is the real relocation
    delta -- read from the code, not guessed. Tables are grouped by extent so
    only a table's own two copies are differenced.
    """
    by_gap: dict = {}
    for lo, hi in pairs:
        by_gap.setdefault(hi - lo, []).append(lo)
    deltas = set()
    for los in by_gap.values():
        inside = [x for x in los if load <= x < end]
        outside = [x for x in los if not (load <= x < end)]
        for a in inside:
            for b in outside:
                if a - b:
                    deltas.add(a - b)
    if load != PLAYERADDR:
        deltas.add(load - PLAYERADDR)
    return sorted(deltas)


def _codescan_meta(data: bytes, base: int, image: SidImage, fmt: SWMFormat):
    """Return ``(swm_header, seq_hint, chord_len, tempo_len)`` for a code scan.

    Uses the ``SWM1`` header when present (lossy metadata + chord/tempo-table
    lengths); otherwise derives the chord/tempo lengths from the code-located
    table bases (chord/tempo are contiguous ahead of the subtune table).
    """
    swm_header = _swm_header(data, base, fmt)
    if swm_header is not None:
        seq_hint = subtunes_for(fmt.field(swm_header, "sequence_amount"), fmt.channels)
        return (
            swm_header,
            seq_hint,
            fmt.field(swm_header, "chord_length"),
            fmt.field(swm_header, "tempo_length"),
        )
    chord_len = tempo_len = 0
    chdt = _code_operands(image, _CODE_CHORDTAB, "chdt")
    tmpt = _code_operands(image, _CODE_TEMPOTAB, "tmpt")
    subt = _subtune_operands(image)
    if len(tmpt) == 1:
        tempo_base = tmpt[0] + 1
        if len(subt) == 1:
            tempo_len = max(0, subt[0] - fmt.restemp_shift - tempo_base)
        if len(chdt) == 1:
            chord_len = max(0, tempo_base - chdt[0])
    elif len(chdt) == 1 and len(subt) == 1:
        chord_len = max(0, (subt[0] - fmt.restemp_shift) - chdt[0])
    return None, None, chord_len, tempo_len


# --- Player-code recovery (run the tune's own init, read its tables) -----------
# When the static readers above cannot resolve a layout -- the tune data is only
# materialised by the player's init (packed/relocating drivers), or the version's
# export is an alternate layout -- the tune is recovered by *running its player*.
# ``run_init`` (jennings) executes the tune's init so the runtime image lands in
# memory exactly as on a C64; every table base is then read straight from the
# player's own instruction operands (never scanned or guessed). The only
# tolerance is that pointer-table slots the tune never plays -- unused, or
# truncated away in a corrupt file's secondary subtunes -- decode to empty; the
# C64 reads the same nothing there. See docs/format.md.


def _image_accessors(image: SidImage):
    """``(byte, chunk)`` accessors reading the materialised image by address."""
    mem = image.mem

    def byte(addr: int) -> int:
        if not 0 <= addr < MEM_SIZE:
            raise _LayoutError(f"address {addr:#06x} out of range")
        return mem[addr]

    def chunk(addr: int, length: int) -> bytes:
        if addr < 0 or addr + length > MEM_SIZE:
            raise _LayoutError(f"slice at {addr:#06x}+{length} out of range")
        return bytes(mem[addr : addr + length])

    return byte, chunk


def _scan_sequence_end(byte, start: int) -> int:
    """Address one past a sequence's ``$FE`` / ``$FF``+loop-byte terminator."""
    addr = start
    guard = 0
    while True:
        cmd = byte(addr)
        addr += 1
        guard += 1
        if guard > _SEQUENCE_GUARD:
            raise _LayoutError("sequence not terminated")
        if cmd == _SEQUENCE_END:
            return addr
        if cmd == _SEQUENCE_END_WITH_LOOP:
            return addr + 1


def _bounded_pattern_end(byte, start: int, ceil: int) -> Optional[int]:
    """Address of a pattern's ``$FF`` row-terminator below ``ceil``, else None.

    Row-aware (an ``$FF`` inside a row's effect data is not the terminator),
    mirroring :func:`_scan_pattern_end` but bounded so an unused/absent slot
    fails softly instead of running away.
    """
    addr = start
    while addr < ceil:
        b = byte(addr)
        if b == TABLE_END:
            return addr
        addr += 1
        if b == 0x00:
            while PACKED_MIN <= byte(addr) <= PACKED_MAX:
                addr += 1
            continue
        if b & 0x80:
            inst_byte = byte(addr)
            addr += 1
            if inst_byte & 0x80:
                fx = byte(addr)
                addr += 1
                if fx < 0x20:
                    addr += 1
    return None


def _direct_channel_bases(image: SidImage, _byte, _subtune: int, fmt: SWMFormat):
    """The channel bases from the ``p_seqtN lda SEQUENCES,y; rts`` operands.

    Self-modified by init to the current subtune's packed per-channel bases (RAM,
    non-subtune-indirect player); the caller re-runs init to select the subtune,
    so this reads the live operands directly.
    """
    direct = [m.captures["seq"] for m in find_code_all(image, _CODE_SEQ_DIRECT)]
    return direct[: fmt.channels] if len(direct) >= fmt.channels else None


def _subtune_channel_bases(image: SidImage, byte, subtune: int, fmt: SWMFormat):
    """The channel bases from the ``SUBTUNES`` pointer table (``p_subt1``).

    Each block holds the subtune's LO/HI sequence pointers
    (subtune-support driver: ``p_seqt`` is the indirect ``lda (zp),y`` form).
    """
    subt = _subtune_operands(image)
    if not subt:
        return None
    blk = subt[0] + fmt.subtune_block * subtune
    return [byte(blk + 2 * c) | (byte(blk + 2 * c + 1) << 8) for c in range(fmt.channels)]


_ORDERLIST_FORMS = (_direct_channel_bases, _subtune_channel_bases)


def _refs(sequences) -> set:
    return {c.pattern for seq in sequences for c in seq if isinstance(c, PlayPattern)}


def _player_patterns(
    byte, chunk, ptn_lo, ptn_hi, pat_amount, ceil, inrange, ref_all, ref_primary, default_len
):
    """Decode patterns; a primary-subtune pattern MUST decode, others may be empty.

    An absent slot becomes an empty pattern of the tune's default length -- SID-
    Wizard's own "empty but lengthy" unused pattern -- so it stays playable.
    """
    empty_len = default_len or 32
    patterns: List[Pattern] = []
    ref_inst: set = set()
    for k in range(1, pat_amount + 1):
        addr = byte(ptn_lo + k) | (byte(ptn_hi + k) << 8)
        term = _bounded_pattern_end(byte, addr, ceil) if inrange(addr) else None
        if term is None:
            if k in ref_primary:
                raise _LayoutError(f"primary-subtune pattern {k} is absent from the image")
            patterns.append(Pattern(rows=[Row() for _ in range(empty_len)]))
            continue
        unpacked = unpack_pattern(chunk(addr, term - addr))
        length = byte(term + 1)
        if not 0 < length <= 0xFF:
            length = len(unpacked)
        pat = Pattern.decode(unpacked, length=length)
        pat.ram = _ram_window(byte, addr)
        patterns.append(pat)
        if k in ref_all:
            for row in pat.rows:
                if row.instrument is not None and row.instrument < 0x40:
                    ref_inst.add(row.instrument)
    return patterns, ref_inst


def _player_instruments(byte, chunk, header, fmt, ordered, ptn_lo, ceil, inrange, ref_inst):
    """Decode instruments from the table below the pattern table.

    The instrument LO/HI pair is the highest matched table below the pattern
    table; when the version's instrument idiom isn't the shared skeleton (V1.0
    interposes a ``jsr``) the tables are derived from the exporter's contiguous
    ``[inst_lo][inst_hi][ptn_lo][ptn_hi]`` layout, trying both plausible widths.
    A referenced instrument must decode; unused/absent slots are empty.
    """
    candidates = []
    below = [(pl, ph) for pl, ph in ordered if pl < ptn_lo]
    if below:
        pl, ph = below[0]
        candidates.append((pl, ph, ph - pl - 1))
    elif header is not None:
        amount = fmt.field(header, "instrument_amount")
        for width in (amount + 1, amount):
            candidates.append((ptn_lo - 2 * width, ptn_lo - width, amount))
    if not candidates:
        raise _LayoutError("no instrument table below the pattern table")

    last: Optional[_LayoutError] = None
    for inst_lo, inst_hi, amount in candidates:
        try:
            return _decode_player_instruments(
                byte, chunk, inst_lo, inst_hi, amount, ceil, inrange, ref_inst
            )
        except _LayoutError as exc:
            last = exc
    raise last


def _decode_player_instruments(byte, chunk, inst_lo, inst_hi, amount, ceil, inrange, ref_inst):
    instruments: List[Instrument] = []
    addrs = [_slot_ptr(byte, inst_lo, inst_hi, k) for k in range(MAX_INSTRUMENT_SLOT + 1)]
    for k in range(1, amount + 1):
        name = b"INST %02d" % k
        addr = addrs[k]
        if not inrange(addr):
            if k in ref_inst:
                raise _LayoutError(f"referenced instrument {k} is absent from the image")
            instruments.append(Instrument(name=name))
            continue
        cursor = _filter_table_end(byte, addr + byte(addr + INST_FILTER_TABLE_PTR_POS), ceil)
        if cursor is None:
            cursor = ceil
        try:
            instruments.append(_decode_at(byte, chunk, addr, cursor, name=name))
        except _LayoutError:
            raise
        except Exception as exc:  # malformed body
            if k in ref_inst:
                raise _LayoutError(f"referenced instrument {k} did not decode: {exc}") from exc
            instruments.append(Instrument(name=name))
    return instruments, _decode_slots(byte, chunk, addrs, amount, ceil, inrange)


# --- Layout location: ``locate()`` strategies yield a Layout; one decode reads it -----------------


class _Escape(Exception):
    """A non-layout error a strategy raised, carried past :func:`locate` (which would log it as a rejection)."""

    def __init__(self, error: SidParseError) -> None:
        super().__init__(str(error))
        self.error = error


def _strict(fn):
    """Reject a candidate only on :class:`_LayoutError`; any other parse error aborts the parse."""

    @wraps(fn)
    def guarded(*args):
        try:
            return fn(*args)
        except _LayoutError:
            raise
        except SidParseError as exc:
            raise _Escape(exc) from exc

    return guarded


def _strict_candidates(fn):
    """:func:`_strict` for a generator of candidate layouts."""

    @wraps(fn)
    def guarded(*args):
        try:
            yield from fn(*args)
        except _LayoutError:
            raise
        except SidParseError as exc:
            raise _Escape(exc) from exc

    return guarded


@dataclass
class _Reader:
    """Address-indexed access to the image a layout is read from."""

    byte: Callable[[int], int]
    chunk: Callable[[int, int], bytes]
    image: Optional[SidImage] = None


@dataclass
class Layout:
    """One candidate placement of a tune's tables and how its decode is bounded.

    The pointer tables (``inst_lo``/``inst_hi``, ``ptn_lo``/``ptn_hi``) and their counts, the
    ``end`` of the tune data, the pointer ``ptr_base`` (the ``SWP`` relative-pointer base) and
    ``reloc`` (a partially-relocated export's delta), and the ``SWM1`` ``header`` that gives the
    lossy metadata when there is one. ``subtunes(min_pattern)`` offers ``(table base, subtunes,
    sequence count)`` candidates once the patterns have fixed where the sequences end; a layout
    with ``orderlists`` is instead read ``tolerant``-ly from the materialised ``image``: slots the
    tune never plays decode empty.
    """

    inst_lo: int
    inst_hi: int
    inst_amount: int
    ptn_lo: int
    ptn_hi: int
    pat_amount: int
    end: int
    load: int
    header: Optional[bytes] = None
    ptr_base: int = 0
    reloc: int = 0
    # The lowest address a table base may take (the load address, where it is known to bound them).
    min_base: Optional[int] = None
    subtunes: Optional[Callable[[int], Iterable[Tuple[int, int, int]]]] = None
    chord_len: int = 0
    tempo_len: int = 0
    # Explicit chord / tempo table bases (the SWP offset table); else derived from the subtune table.
    chord_base: Optional[int] = None
    tempo_base: Optional[int] = None
    # Prefer the chord / tempo bases baked into the player code over the subtune-table geometry.
    scan: bool = False
    author: Optional[bytes] = None
    # Tolerant read: the orderlist bases, how many are real, the data bounds and the subtune table.
    orderlists: Optional[List[Optional[int]]] = None
    seq_amount: int = 0
    floor: int = 0
    ceil: int = 0
    ordered: Optional[list] = None
    subtune_count: int = 1
    subtune_table: Optional[int] = None
    image: Optional[SidImage] = None
    fmt: SWMFormat = SWM1_FORMAT

    @property
    def tolerant(self) -> bool:
        return self.orderlists is not None


class _Tables(NamedTuple):
    """The regions one decode read, ready for :func:`_assemble`."""

    sequences: list
    patterns: list
    instruments: list
    slots: Optional[Dict[int, Instrument]]
    subtune_base: Optional[int]
    subtunes: int
    min_pattern: int


def _swm_header(data: bytes, base: int, fmt: SWMFormat) -> Optional[bytes]:
    """The ``fmt`` tune header at or after ``base``, or None when absent or truncated."""
    pos = data.find(fmt.magic, base)
    if 0 <= pos and pos + fmt.header_size <= len(data):
        return data[pos : pos + fmt.header_size]
    return None


def _locate_one(byte, inst_lo, subtunes, seq_amount, load, fmt):
    """``Layout.subtunes`` for the one subtune table the header geometry puts below ``inst_lo``."""

    def candidates(min_pattern: int):
        yield (
            _locate_subtune_table(byte, inst_lo, subtunes, seq_amount, load, min_pattern, fmt),
            subtunes,
            seq_amount,
        )

    return candidates


def _swm_layouts(data: bytes, load: int, base: int, header: bytes, fmt: SWMFormat):
    """Layouts for each end-of-data the ``SWM1`` header could mean, from the file end downward.

    The pattern-hi pointer table ends the tune data, but some files append a small
    relocation/player stub after it, so the true end is probed downward from the file end.
    """
    seq_amount = fmt.field(header, "sequence_amount")
    pat_amount = fmt.field(header, "pattern_amount")
    inst_amount = fmt.field(header, "instrument_amount")
    subtunes = subtunes_for(seq_amount, fmt.channels)
    byte, _ = _memory_accessors(data, load, base)
    for end in range(load + (len(data) - base), load, -1):
        # Pointer-table bases, derived backward from end-of-data (exporter math).
        ptn_hi = end - (pat_amount + 1)
        ptn_lo = ptn_hi - pat_amount
        inst_hi = ptn_lo - inst_amount
        inst_lo = inst_hi - (inst_amount + 1)
        yield Layout(
            inst_lo,
            inst_hi,
            inst_amount,
            ptn_lo,
            ptn_hi,
            pat_amount,
            end,
            load,
            header=header,
            min_base=load,
            subtunes=_locate_one(byte, inst_lo, subtunes, seq_amount, load, fmt),
            chord_len=fmt.field(header, "chord_length"),
            tempo_len=fmt.field(header, "tempo_length"),
            fmt=fmt,
        )


def _swp_layout(data: bytes, load: int, base: int, swp_off: int, fmt: SWMFormat) -> Layout:
    """The layout a "packed" (``SWP``) export's ``SWP1`` offset-table gives.

    The tune data is the ordinary runtime layout; only the pointer-table bases and pointer values
    differ. The bases are read from the ``SWP1`` offset-table (:data:`_SWP_TABLE_OFFSETS`) and every
    table entry is a pointer relative to ``swp_base`` (the C64 address of the ``SWP1`` magic).
    """
    swp_base = load + (swp_off - base)
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
    subtunes = (min(higher) - sub_off) // fmt.subtune_block
    inst_amount = offs["ptn_lo"] - offs["inst_hi"]
    pat_amount = offs["ptn_hi"] - offs["ptn_lo"]
    if subtunes < 1 or inst_amount < 0 or pat_amount < 0:
        raise _LayoutError(
            f"implausible SWP counts: subtunes={subtunes}, "
            f"instruments={inst_amount}, patterns={pat_amount}"
        )

    # [chord][tempo][8B RESTEMP][subtunes]; SWM1 header is not trusted for lengths
    chord_len = offs["tempo_table"] - offs["chord_table"]
    tempo_len = offs["subtunes"] - offs["tempo_table"] - fmt.restemp_shift
    if chord_len < 0 or tempo_len < 0:
        raise _LayoutError(
            f"implausible SWP chord/tempo lengths: chord={chord_len}, tempo={tempo_len}"
        )

    # The tune's real author is in the SWP1 block; the SWM1 header's is the template's.
    auth_end = swp_off + AUTHOR_POS + AUTHOR_LEN
    author = None
    if auth_end <= len(data) and data[swp_off + AUTHOR_POS : auth_end].rstrip(b" \x00"):
        author = bytes(data[swp_off + AUTHOR_POS : auth_end])

    subtune_base = swp_base + offs["subtunes"]
    seq_amount = subtunes * fmt.channels
    return Layout(
        swp_base + offs["inst_lo"],
        swp_base + offs["inst_hi"],
        inst_amount,
        swp_base + offs["ptn_lo"],
        swp_base + offs["ptn_hi"],
        pat_amount,
        end,
        load,
        header=_swm_header(data, base, fmt),
        ptr_base=swp_base,
        subtunes=lambda _min_pattern: [(subtune_base, subtunes, seq_amount)],
        chord_len=chord_len,
        tempo_len=tempo_len,
        chord_base=swp_base + offs["chord_table"],
        tempo_base=swp_base + offs["tempo_table"],
        author=author,
        fmt=fmt,
    )


def _codescan_layouts(image: SidImage, data: bytes, load: int, base: int, fmt: SWMFormat):
    """Layouts from the table bases baked into the player code's operands.

    Relocation-invariant fallback used when the ``SWM1`` header end-probe cannot resolve the layout
    (relocated / alternate-layout exports) and for magic-less exports that carry no ``SWM1`` /
    ``SWP1`` at all. Reads the pattern- and instrument-pointer table bases (and counts) from the
    player's own indexed-load instructions; relocation deltas are read from the code
    (:func:`_reloc_delta_candidates`), not guessed. Each pairing is a candidate; only one that decodes
    to a validated, pattern-ref-coherent layout is accepted.
    """
    byte, _ = _memory_accessors(data, load, base)
    end = load + (len(data) - base)
    pairs = sorted(
        {(m.captures["lo"], m.captures["hi"]) for m in find_code_all(image, _CODE_PTRTAB)}
    )
    if len(pairs) < 2:
        raise _LayoutError(f"code-scan: found {len(pairs)} pointer-table access sites, need 2")
    chdp = _code_operands(image, _CODE_CHORDPTR, "chdp")
    tmpp = _code_operands(image, _CODE_TEMPOPTR, "tmpp")
    header, seq_hint, chord_len, tempo_len = _codescan_meta(data, base, image, fmt)

    ordered = sorted(pairs, key=lambda pair: pair[1], reverse=True)
    relocs = [0, *_reloc_delta_candidates(pairs, load, end)]
    tried: set = set()
    for reloc in relocs:
        if reloc in tried:
            continue
        tried.add(reloc)
        for ptn_lo, ptn_hi in ordered:
            for inst_lo, inst_hi in ordered:
                if inst_lo >= ptn_lo:
                    continue

                def subtunes(min_pattern, inst_lo=inst_lo, reloc=reloc):
                    return (
                        (sb, n, n * fmt.channels)
                        for sb, n in _iter_subtune_bases(
                            byte,
                            image,
                            inst_lo,
                            min_pattern,
                            load,
                            end,
                            seq_hint,
                            chdp,
                            tmpp,
                            reloc,
                            fmt,
                        )
                    )

                # Counts come from the table extents baked into the player code, so no header count
                # is trusted.
                yield Layout(
                    inst_lo,
                    inst_hi,
                    inst_hi - inst_lo - 1,
                    ptn_lo,
                    ptn_hi,
                    ptn_hi - ptn_lo,
                    end,
                    load,
                    header=header,
                    reloc=reloc,
                    min_base=load,
                    subtunes=subtunes,
                    chord_len=chord_len,
                    tempo_len=tempo_len,
                    scan=True,
                    fmt=fmt,
                )


def _player_layouts(data: bytes, load: int, base: int, fmt: SWMFormat):
    """The layout of a tune recovered by running its player and reading its own table operands.

    Final fallback for tunes the static readers cannot resolve: ``run_init`` executes the tune's init
    so the runtime image lands in memory as on a C64, and every table base is read from the player's
    own instruction operands. Yields nothing unless the player yields a coherent, orderlist-coherent
    layout.
    """
    image = SidImage.from_sid(data)
    try:
        run_init(image, 0)
    except Exception as exc:  # EmulatorUnavailable / SidParseError
        raise _LayoutError(f"could not run the tune's init: {exc}") from exc
    byte, chunk = _image_accessors(image)

    pairs = sorted(
        {(m.captures["lo"], m.captures["hi"]) for m in find_code_all(image, _CODE_PTRTAB)}
    )
    if not pairs:
        raise _LayoutError("no pointer-table access sites in the player code")
    data_hi = min(b for pair in pairs for b in pair)
    ordered = sorted(pairs, key=lambda p: p[1], reverse=True)

    header = _swm_header(data, base, fmt)
    seq_amount = fmt.field(header, "sequence_amount") if header is not None else fmt.channels
    subtunes = subtunes_for(seq_amount, fmt.channels)

    # Pick the orderlist form (direct or subtune-table) and the pattern table
    # together: the combination whose primary-subtune orderlists reference only
    # patterns the table resolves. This fixes the data floor and rejects the
    # stale default ``p_seqt`` operands some drivers leave alongside the real
    # subtune table.
    form = ptn_lo = ptn_hi = pat_amount = None
    data_lo = data_hi
    for candidate in _ORDERLIST_FORMS:
        bases0 = candidate(image, byte, 0, fmt)
        if not bases0:
            continue
        floor = min(bases0)

        def inrange0(addr: int, floor=floor) -> bool:
            return floor <= addr < data_hi

        try:
            seqs0 = [decode_sequence(chunk(b, _scan_sequence_end(byte, b) - b)) for b in bases0]
        except _LayoutError:
            continue
        refs0 = _refs(seqs0)
        for pl, ph in ordered:
            n = ph - pl
            if (
                0 < n < 256
                and refs0
                and max(refs0) <= n
                and all(inrange0(byte(pl + p) | (byte(ph + p) << 8)) for p in refs0 if p)
            ):
                form, ptn_lo, ptn_hi, pat_amount, data_lo = candidate, pl, ph, n, floor
                break
        if form is not None:
            break
    if form is None:
        raise _LayoutError("no orderlist form resolves against a pattern table")

    # Collect every subtune's sequences; only the header's seq_amount slots are
    # real orderlists, the rest are unused (empty). Re-init per subtune so the
    # direct-form p_seqt operands point at each subtune's bases.
    seq_slots = []
    for st in range(subtunes):
        run_init(image, st)
        seq_slots.extend(form(image, byte, st, fmt) or [None] * fmt.channels)
    real = [
        b if i < seq_amount and b is not None and data_lo <= b < data_hi else None
        for i, b in enumerate(seq_slots)
    ]
    materialised = [b for b in real if b is not None]
    data_lo = min(materialised) if materialised else data_lo
    run_init(image, 0)

    subt = _subtune_operands(image)
    yield Layout(
        0,
        0,
        0,
        ptn_lo,
        ptn_hi,
        pat_amount,
        data_hi,
        load,
        header=header,
        chord_len=fmt.field(header, "chord_length") if header is not None else 0,
        tempo_len=fmt.field(header, "tempo_length") if header is not None else 0,
        scan=True,
        orderlists=real,
        seq_amount=seq_amount,
        floor=data_lo,
        ceil=data_hi,
        ordered=ordered,
        subtune_count=subtunes,
        subtune_table=subt[0] if subt else None,
        image=image,
        fmt=fmt,
    )


def _read_strict(lay: Layout, rd: _Reader) -> _Tables:
    """Read a layout whose sequences hang off a subtune table: every region must decode."""
    instruments, slots = _decode_instruments(
        rd.byte,
        rd.chunk,
        lay.inst_lo,
        lay.inst_hi,
        lay.inst_amount,
        lay.load,
        lay.end,
        ptr_base=lay.ptr_base,
        reloc_delta=lay.reloc,
    )
    patterns, min_pattern = _decode_patterns(
        rd.byte,
        rd.chunk,
        lay.ptn_lo,
        lay.ptn_hi,
        lay.pat_amount,
        lay.load,
        lay.end,
        ptr_base=lay.ptr_base,
        reloc_delta=lay.reloc,
    )
    last: Optional[_LayoutError] = None
    # A false-positive subtune block is skipped, not mis-parsed: keep the first whose orderlist
    # pattern references all resolve.
    for subtune_base, subtunes, seq_amount in lay.subtunes(min_pattern):
        try:
            sequences = _decode_sequences(
                rd.byte,
                rd.chunk,
                subtune_base,
                seq_amount,
                lay.load,
                lay.end,
                lay.fmt,
                ptr_base=lay.ptr_base,
                reloc_delta=lay.reloc,
            )
            _validate_pattern_refs(sequences, len(patterns))
        except _LayoutError as exc:
            last = exc
            continue
        return _Tables(sequences, patterns, instruments, slots, subtune_base, subtunes, min_pattern)
    raise last or _LayoutError("no coherent subtune table")


def _read_tolerant(lay: Layout, rd: _Reader) -> _Tables:
    """Read a layout from the materialised player image.

    The only tolerance is that slots the tune never plays -- unused, or truncated away in a corrupt
    file's secondary subtunes -- decode to empty; the C64 reads the same nothing there.
    """

    def inrange(addr: int) -> bool:
        return lay.floor <= addr < lay.ceil

    # Emit exactly seq_amount orderlists (the header's count); the final subtune
    # may hold fewer than fmt.channels. A slot with no materialised base is an
    # empty End (unused, or truncated in a corrupt file's secondary subtune).
    sequences, ref_all, ref_primary = [], set(), set()
    for i, b in enumerate(lay.orderlists[: lay.seq_amount]):
        if b is None:
            sequences.append([End()])
            continue
        seq = decode_sequence(rd.chunk(b, _scan_sequence_end(rd.byte, b) - b))
        sequences.append(seq)
        this = {c.pattern for c in seq if isinstance(c, PlayPattern)}
        ref_all |= this
        if i < lay.fmt.channels:
            ref_primary |= this

    default_len = (
        lay.fmt.field(lay.header, "default_pattern_length") if lay.header is not None else 0
    )
    patterns, ref_inst = _player_patterns(
        rd.byte,
        rd.chunk,
        lay.ptn_lo,
        lay.ptn_hi,
        lay.pat_amount,
        lay.ceil,
        inrange,
        ref_all,
        ref_primary,
        default_len,
    )
    instruments, slots = _player_instruments(
        rd.byte, rd.chunk, lay.header, lay.fmt, lay.ordered, lay.ptn_lo, lay.ceil, inrange, ref_inst
    )
    _validate_pattern_refs(sequences, len(patterns))
    return _Tables(sequences, patterns, instruments, slots, None, lay.subtune_count, lay.floor)


def _subtune_tempos(lay: Layout, rd: _Reader, tables: _Tables) -> list:
    """Per-subtune funktempo ``(left, right)`` pairs from the ``SUBTUNES`` table's blocks."""
    base = lay.subtune_table if lay.tolerant else tables.subtune_base
    out = []
    for st in range(tables.subtunes):
        if base is None:
            out.append((0, 0))
            continue
        blk = base + lay.fmt.subtune_block * (st + 1) - 2
        try:
            out.append((rd.byte(blk), rd.byte(blk + 1)))
        except _LayoutError:
            if not lay.tolerant:
                raise
            out.append((0, 0))
    return out


def _chord_tempo(lay: Layout, rd: _Reader, tables: _Tables) -> Tuple[bytes, bytes]:
    """The chord and tempo table bytes: explicit bases, the player code's operands, or the geometry
    below the subtune table (with the 8-byte RESTEMP gap the exporter inserts ahead of the tempo
    programs)."""
    if lay.chord_base is not None:
        chord = rd.chunk(lay.chord_base, lay.chord_len) if lay.chord_len else b""
        tempo = rd.chunk(lay.tempo_base, lay.tempo_len) if lay.tempo_len else b""
        return chord, tempo
    subtune_base, min_pattern = tables.subtune_base, tables.min_pattern
    if lay.tolerant:
        subtune_base = lay.floor + _SUBTUNE_SCAN_WINDOW
    if lay.scan:
        return _codescan_chord_tempo(
            rd.image, rd.chunk, subtune_base, min_pattern, lay.chord_len, lay.tempo_len, lay.fmt
        )
    chord_base = subtune_base - lay.fmt.restemp_shift - lay.tempo_len - lay.chord_len
    if chord_base <= min_pattern:
        raise _LayoutError("chord table overlaps the pattern region")
    chord = rd.chunk(chord_base, lay.chord_len) if lay.chord_len else b""
    tempo = (
        rd.chunk(chord_base + lay.chord_len + lay.fmt.restemp_shift, lay.tempo_len)
        if lay.tempo_len
        else b""
    )
    return chord, tempo


def _assemble(lay: Layout, tables: _Tables, rd: _Reader) -> SWMFile:
    """Build the :class:`SWMFile` from the decoded regions and the tune header.

    ``lay.header`` carries the lossy metadata (frame speed, author, driver / tuning type, ...). It
    is ``None`` for an export that omits the ``SWM1`` header, in which case the model's own field
    defaults are kept.
    """
    header = lay.header
    meta = lay.fmt.header_meta(header) if header is not None else {"sid_amount": lay.fmt.sid_amount}
    subtune_tempos = _subtune_tempos(lay, rd, tables)
    chord_table, tempo_table = _chord_tempo(lay, rd, tables)
    slots = tables.slots
    if lay.author is not None:
        meta["author"] = lay.author
    return SWMFile(
        sequences=tables.sequences,
        patterns=tables.patterns,
        instruments=tables.instruments,
        chord_table=chord_table,
        tempo_table=tempo_table,
        subtune_tempos=subtune_tempos,
        load_address=lay.load,
        player=(
            PlayerImage(
                features_for_driver(meta.get("driver_type", 0)),
                dummy_instrument=slots.get(0),
                spare_instruments={k: ins for k, ins in slots.items() if k},
            )
            if slots
            else None
        ),
        **meta,
    )


def _decode_layout(lay: Layout, rd: _Reader) -> SWMFile:
    """The one decode and validation every layout strategy shares."""
    if (
        lay.inst_amount < 0
        or lay.pat_amount < 0
        or (lay.min_base is not None and lay.inst_lo <= lay.min_base)
    ):
        raise _LayoutError("implausible table geometry")
    tables = _read_tolerant(lay, rd) if lay.tolerant else _read_strict(lay, rd)
    return _assemble(lay, tables, rd)


def _static_strategy(name: str, data: bytes, load: int, base: int, layouts) -> Strategy:
    """A strategy reading ``layouts(image)`` out of the file's own bytes."""

    @_strict
    def decode(image: SidImage, lay: Layout) -> SWMFile:
        byte, chunk = _memory_accessors(data, load, base)
        return _decode_layout(lay, _Reader(byte, chunk, image))

    return Strategy(name, STATIC, decode, _strict_candidates(layouts))


def _swp_strategy(data: bytes, load: int, base: int, swp_off: int, fmt: SWMFormat) -> Strategy:
    return _static_strategy(
        "swp", data, load, base, lambda image: [_swp_layout(data, load, base, swp_off, fmt)]
    )


def _swm_strategy(data: bytes, load: int, base: int, header: bytes, fmt: SWMFormat) -> Strategy:
    return _static_strategy(
        "swm-header", data, load, base, lambda image: _swm_layouts(data, load, base, header, fmt)
    )


def _codescan_strategy(data: bytes, load: int, base: int, fmt: SWMFormat) -> Strategy:
    return _static_strategy(
        "codescan",
        data,
        load,
        base,
        lambda image: _codescan_layouts(image, data, load, base, fmt),
    )


def _player_strategy(data: bytes, load: int, base: int, fmt: SWMFormat) -> Strategy:
    @_strict
    def decode(_image: SidImage, lay: Layout) -> SWMFile:
        byte, chunk = _image_accessors(lay.image)
        return _decode_layout(lay, _Reader(byte, chunk, lay.image))

    return Strategy(
        "via-player",
        STATIC,
        decode,
        _strict_candidates(lambda image: _player_layouts(data, load, base, fmt)),
    )


def _record_layout(swm: SWMFile, strategy: str, source: Source) -> None:
    """Note on ``swm``'s player image the ``strategy`` that located its tables."""
    player = swm.player or PlayerImage(features_for_driver(swm.driver_type))
    swm.player = replace(
        player, layout=strategy, provenance={**player.provenance, VARIANT_FACET: source}
    )


def _locate_swm(image: SidImage, strategies: List[Strategy]) -> Located:
    """:func:`locate` over ``strategies``, with a non-layout parse error propagating as raised."""
    try:
        return locate(image, strategies)
    except _Escape as escape:
        raise escape.error from None


def _last_rejection(located: Located, strategy: str) -> Optional[str]:
    """Why ``strategy``'s last candidate was rejected."""
    reasons = [r.reason for r in located.rejections if r.strategy == strategy]
    return reasons[-1].removeprefix("candidates: ") if reasons else None
