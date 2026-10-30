"""The byte grammar of the streams SID-Wizard's player reads (``sidwizard.lark``).

Each stream kind is one start rule; a stream that does not parse raises
:class:`~pysidwizard.errors.SWMFormatError` instead of being mis-played.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterator, List

from lark import Token, Transformer
from lark.lark import PostLex
from pysidtracker.bytegrammar import ByteGrammar, Parsed, Stream

from .constants import (
    PACKED_MAX,
    PACKED_MIN,
    SEQUENCE_END_WITH_LOOP,
    SEQUENCE_SUBTUNE_JUMP_BASE,
    SEQUENCE_TEMPO_BASE,
    SEQUENCE_TRANSPOSE_BASE,
)
from .errors import SWMFormatError
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
    TempoOverride,
    Transpose,
    encode_sequence,
    pack_pattern,
)

RULES = ("sequence", "pattern", "instrument", "chords", "tempos")


def _end(tokens) -> int:
    """The offset one past the last byte the tokens span."""
    return max((tok.end_pos for tok in tokens if isinstance(tok, Token)), default=0)


def _flat(items) -> List[Token]:
    out: List[Token] = []
    for item in items:
        out.extend(_flat(item) if isinstance(item, list) else [item])
    return out


class _Transformer(Transformer):
    """Builds each stream's model while it parses."""

    def sequence(self, children) -> Parsed:
        commands: List[SequenceCommand] = []
        loop = False
        for tok in children:
            value = ord(tok)
            if loop:
                loop = False
                commands.append(
                    SubtuneJump(byte=value)
                    if value >= SEQUENCE_SUBTUNE_JUMP_BASE
                    else Loop(position=value)
                )
            elif tok.type == "PLAY":
                commands.append(PlayPattern(value))
            elif tok.type == "TRANSPOSE":
                commands.append(Transpose(value - SEQUENCE_TRANSPOSE_BASE))
            elif tok.type == "MAINVOL":
                commands.append(MainVolume(value & 0x0F))
            elif tok.type == "TEMPO":
                commands.append(TempoOverride(value - SEQUENCE_TEMPO_BASE))
            elif tok.type == "NOP":
                commands.append(RawSequenceByte(value))
            elif value == SEQUENCE_END_WITH_LOOP:
                loop = True
            else:
                commands.append(End())
        return Parsed(commands, _end(children))

    def pattern(self, children) -> Parsed:
        rows: List[Row] = []
        for item in children[:-1]:
            rows.extend(item if isinstance(item, list) else _zero_rows(_expand([item])))
        return Parsed(rows, _end(children))

    def note_row(self, children):
        return [Row(note=ord(children[0]))]

    def empty_row(self, children):
        return [Row()]

    def inst_row(self, children):
        (instrument, fx, value), rest = children[1]
        note = ord(children[0]) & 0x7F
        return [Row(note=note or None, instrument=instrument, fx=fx, fx_value=value)] + rest

    def inst_only(self, children):
        return (ord(children[0]), None, None), []

    def inst_zero(self, children):
        return (None, None, None), []

    def inst_fx(self, children):
        (fx, value), rest = children[1]
        return (ord(children[0]) & 0x7F or None, fx, value), rest

    def fx_small(self, children):
        return (ord(children[0]), None), []

    def fx_big(self, children):
        value, rest = children[1]
        return (ord(children[0]), value), rest

    def fx_zero(self, children):
        value, rest = children[1]
        return (0, value), rest

    def val_nz(self, children):
        return ord(children[0]), []

    def val_zero(self, children):
        return 0, []

    def val_packed(self, children):
        return 0, _zero_rows(_expand(children) - 1)

    def header(self, children) -> bytes:
        return bytes(ord(tok) for tok in children)

    def tab_row(self, children) -> List[Token]:
        return list(children)

    def table(self, children) -> bytes:
        return bytes(ord(tok) for tok in _flat(children))

    def instrument(self, children) -> Parsed:
        header, wf, pw, filt = children[:4]
        wf_end = len(header) + len(wf)
        pw_ptr, filt_ptr = header[10], header[11]
        if (pw_ptr, filt_ptr) != (wf_end, wf_end + len(pw)):
            raise SWMFormatError(
                f"instrument table pointers {pw_ptr}/{filt_ptr} do not match "
                f"its tables at {wf_end}/{wf_end + len(pw)}"
            )
        body = header + wf + pw + filt
        return Parsed(Instrument.decode(body[:-1], name=b""), len(body))

    def chords(self, children) -> Parsed:
        data = bytes(b for chord in children for b in chord)
        return Parsed(data, len(data))

    def chord(self, children) -> List[int]:
        return [ord(tok) for tok in children]

    program = chord

    def tempos(self, children) -> Parsed:
        data = bytes(b for program in children for b in program)
        return Parsed(data, len(data))


def _zero_rows(count: int) -> List[Row]:
    return [Row() for _ in range(count)]


def _expand(packs) -> int:
    """The zero rows the ``$70..$77`` runs stand for."""
    return sum(ord(tok) - PACKED_MIN + 2 for tok in packs)


_PATTERN_BYTES = frozenset({"NOTE", "INST_NZ", "FX_SMALL", "VAL_NZ", "PACK"})


class _PackPostLex(PostLex):
    """Retypes a pattern ``$70..$77`` byte after a zero or another ``$70..$77`` byte as ``PACK``."""

    def process(self, stream: Iterator[Token]) -> Iterator[Token]:
        prev = -1
        for tok in stream:
            if len(tok) != 1:
                yield tok
                continue
            value = ord(tok)
            if (
                tok.type in _PATTERN_BYTES
                and PACKED_MIN <= value <= PACKED_MAX
                and (prev == 0 or PACKED_MIN <= prev <= PACKED_MAX)
            ):
                tok = Token.new_borrow_pos("PACK", str(tok), tok)
            prev = value
            yield tok


GRAMMAR = ByteGrammar("pysidwizard", "sidwizard.lark", RULES, _Transformer(), error=SWMFormatError)
GRAMMAR.postlex = _PackPostLex()


def parse_sequence(data: bytes) -> List[SequenceCommand]:
    """An orderlist's commands; bytes stored after its terminator are kept raw."""
    commands, size = GRAMMAR.parse_prefix(data, "sequence")
    return commands + [RawSequenceByte(b) for b in data[size:]]


def parse_pattern(packed: bytes, length: int) -> Pattern:
    """The pattern a NOP-packed body (``$FF`` terminator implicit) holds."""
    return Pattern(rows=GRAMMAR.parse_exact(packed + b"\xff", "pattern"), length=length)


def parse_instrument(body: bytes, name: bytes) -> Instrument:
    """The instrument a body (filter-table ``$FF`` implicit) holds."""
    return replace(GRAMMAR.parse_exact(body + b"\xff", "instrument"), name=bytes(name))


def streams(swm) -> Iterator[Stream]:
    """``(name, model, rule)`` for each stream of ``swm``, as the editor wrote it."""
    for i, seq in enumerate(swm.sequences):
        yield f"sequence {i}", _Bytes(encode_sequence(seq)), "sequence"
    for i, pat in enumerate(swm.patterns, 1):
        if pat.size_override != 0:
            yield f"pattern {i}", _Bytes(pack_pattern(pat.encode_unpacked()) + b"\xff"), "pattern"
    for i, ins in enumerate(swm.instruments, 1):
        yield f"instrument {i}", _Bytes(ins.encode() + b"\xff"), "instrument"
    yield "chord table", _Bytes(swm.chord_table), "chords"
    yield "tempo table", _Bytes(swm.tempo_table), "tempos"


class _Bytes:
    """A stream's bytes, as :meth:`ByteGrammar.listing` reads a model."""

    def __init__(self, data: bytes):
        self.data = data

    def to_bytes(self) -> bytes:
        return self.data
