"""The byte grammar of the streams SID-Wizard's player reads (``sidwizard.lark``).

Each stream kind is one start rule; a stream that does not parse raises
:class:`~pysidwizard.errors.SWMFormatError` instead of being mis-played.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Dict, Iterator, List, Optional

from lark import Token, Transformer, v_args
from pysidtracker.bytegrammar import BitmapPostLex, ByteGrammar, Parsed, Stream

from .constants import (
    PACKED_MAX,
    PACKED_MIN,
    SEQUENCE_MAINVOL_BASE,
    SEQUENCE_TEMPO_BASE,
    SEQUENCE_TRANSPOSE_BASE,
    TABLE_SET_FLAG,
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
from .typed import (
    AbsolutePitch,
    BigEffect,
    Chord,
    ChordPitch,
    FilterSet,
    FilterSweep,
    GateOff,
    GateOn,
    KeepPitch,
    Legato,
    Note,
    NoteEffect,
    Portamento,
    PulseSet,
    PulseSweep,
    RelativePitch,
    RepeatRow,
    RingOff,
    RingOn,
    SelectInstrument,
    SmallEffect,
    SyncOff,
    SyncOn,
    TableJump,
    TempoProgram,
    VibratoAmplitude,
    WaveRow,
    signed_byte,
)

RULES = ("sequence", "pattern", "instrument", "chords", "tempos")
TABLE_RULES = ("wf_table", "pw_table", "filter_table")

_SEQUENCE: Dict[str, Callable[[int], SequenceCommand]] = {
    "PLAY": PlayPattern,
    "TRANSPOSE": lambda b: Transpose(b - SEQUENCE_TRANSPOSE_BASE),
    "MAINVOL": lambda b: MainVolume(b - SEQUENCE_MAINVOL_BASE),
    "TEMPO": lambda b: TempoOverride(b - SEQUENCE_TEMPO_BASE),
    "NOP": RawSequenceByte,
    "SEQ_END": lambda b: End(),
    "LOOP_POSITION": Loop,
    "SUBTUNE_JUMP": SubtuneJump,
}

_Kind = Optional[Callable[[int], int]]

_NOTE_COLUMN: Dict[str, _Kind] = {
    "EMPTY": None,
    "NOTE": Note,
    "VIBRATO_AMP": VibratoAmplitude,
    "PORTAMENTO": Portamento,
    "SYNC_ON": SyncOn,
    "SYNC_OFF": SyncOff,
    "RING_ON": RingOn,
    "RING_OFF": RingOff,
    "GATE_ON": GateOn,
    "GATE_OFF": GateOff,
    "NOTE_FX": NoteEffect,
}
_INST_COLUMN: Dict[str, _Kind] = {
    "NO_INST": None,
    "INSTRUMENT": SelectInstrument,
    "LEGATO": Legato,
    "INST_FX": SmallEffect,
}
_COLUMNS: Dict[str, _Kind] = {
    **_NOTE_COLUMN,
    **_INST_COLUMN,
    "NO_FX": int,
    "BIG_FX": BigEffect,
    "SMALL_FX": SmallEffect,
    "VALUE": int,
    "PACK": lambda b: 0,
    "PITCH_UP": RelativePitch,
    "PITCH_DOWN": RelativePitch,
    "CHORD": ChordPitch,
    "KEEP_PITCH": KeepPitch,
    "ABS_PITCH": AbsolutePitch,
    "DETUNE": int,
    "KEEP_DETUNE": None,
}

# Bit 7 of a note or instrument byte announces the next column.
_NEXT_COLUMN = {
    **{name: ((7, "_INST", 0),) for name in _NOTE_COLUMN},
    **{name: ((7, "_FX", 0),) for name in _INST_COLUMN},
}


_MASKS = (0x7F, 0x7F, 0xFF, 0xFF)


def _column(tok: Token, mask: int = 0xFF):
    """``tok``'s byte (``mask``-ed) as its terminal's column type; ``None`` for none."""
    kind = _COLUMNS[tok.type]
    return None if kind is None else kind(ord(tok) & mask)


def _end(tokens) -> int:
    """The offset one past the last byte the tokens span."""
    return max((tok.end_pos for tok in tokens if isinstance(tok, Token)), default=0)


def _zero_rows(count: int) -> List[Row]:
    return [Row() for _ in range(count)]


def _expand(tok: Token) -> int:
    """The zero rows a ``$70..$77`` run stands for."""
    return ord(tok) - PACKED_MIN + 2


def _table(children) -> Parsed:
    return Parsed(children[:-1], _end(children))


class _Transformer(Transformer):
    """Builds each stream's typed model while it parses."""

    def sequence(self, children) -> Parsed:
        commands = [_SEQUENCE[tok.type](ord(tok)) for tok in children if tok.type in _SEQUENCE]
        return Parsed(commands, _end(children))

    def pattern(self, children) -> Parsed:
        rows: List[Row] = []
        for item in children[:-1]:
            rows.extend(item if isinstance(item, list) else _zero_rows(_expand(item)))
        return Parsed(rows, _end(children))

    def row(self, children) -> List[Row]:
        cols = [_column(tok, mask) for tok, mask in zip(children, _MASKS, strict=False)]
        packed = len(children) == 4 and children[3].type == "PACK"
        return [Row(*cols)] + _zero_rows(_expand(children[3]) - 1 if packed else 0)

    def header(self, children) -> bytes:
        return bytes(ord(tok) for tok in children)

    wf_table = pw_table = filter_table = staticmethod(_table)

    def wf(self, children) -> list:
        return children[:-1]

    pw = filt = wf

    @v_args(inline=True)
    def wave_row(self, wave, pitch, detune) -> WaveRow:
        return WaveRow(ord(wave), _column(pitch), _column(detune))

    @v_args(inline=True)
    def repeat_row(self, count, pitch, detune) -> RepeatRow:
        return RepeatRow(ord(count), _column(pitch), _column(detune))

    @v_args(inline=True)
    def pulse_sweep(self, frames, step, keytrack) -> PulseSweep:
        return PulseSweep(ord(frames), signed_byte(ord(step)), ord(keytrack))

    @v_args(inline=True)
    def pulse_set(self, high, low, keytrack) -> PulseSet:
        return PulseSet(ord(high) & ~TABLE_SET_FLAG, ord(low), ord(keytrack))

    @v_args(inline=True)
    def filter_sweep(self, frames, step, keytrack) -> FilterSweep:
        return FilterSweep(ord(frames), signed_byte(ord(step)), ord(keytrack))

    @v_args(inline=True)
    def filter_set(self, control, cutoff, keytrack) -> FilterSet:
        ctrl = ord(control)
        return FilterSet(ctrl & 0x70, ctrl & 0x0F, ord(cutoff), ord(keytrack))

    @v_args(inline=True)
    def table_jump(self, _jump, target, pad) -> TableJump:
        return TableJump(ord(target), ord(pad))

    def instrument(self, children) -> Parsed:
        header, *tables = children[:4]
        body = header + b"\xff".join(b"".join(row.encode() for row in t) for t in tables)
        return Parsed(Instrument.decode(body, name=b"", strict=False), len(body) + 1)

    def chords(self, children) -> Parsed:
        return Parsed(children, sum(len(chord.encode()) for chord in children))

    def chord(self, children) -> Chord:
        return Chord(
            tuple(signed_byte(ord(tok)) for tok in children[:-1]), children[-1].type == "CHORD_LOOP"
        )

    def tempos(self, children) -> Parsed:
        return Parsed(children, sum(len(program.tempos) for program in children))

    def tempo_program(self, children) -> TempoProgram:
        return TempoProgram(tuple(ord(tok) & 0x7F for tok in children))


_PACKABLE = frozenset({"VIBRATO_AMP", "VALUE"})


class _PatternPostLex(BitmapPostLex):
    """Retypes a pattern ``$70..$77`` byte after a zero or another ``$70..$77`` byte as
    ``PACK``, then tags each note and instrument byte whose bit 7 announces the next column."""

    def process(self, stream: Iterator[Token]) -> Iterator[Token]:
        return super().process(self._packs(stream))

    @staticmethod
    def _packs(stream: Iterator[Token]) -> Iterator[Token]:
        prev = -1
        for tok in stream:
            if len(tok) != 1:
                yield tok
                continue
            value = ord(tok)
            if (
                tok.type in _PACKABLE
                and PACKED_MIN <= value <= PACKED_MAX
                and (prev == 0 or PACKED_MIN <= prev <= PACKED_MAX)
            ):
                tok = Token.new_borrow_pos("PACK", str(tok), tok)
            prev = value
            yield tok


GRAMMAR = ByteGrammar(
    "pysidwizard",
    "sidwizard.lark",
    RULES + TABLE_RULES,
    _Transformer(),
    error=SWMFormatError,
)
GRAMMAR.postlex = _PatternPostLex(_NEXT_COLUMN)


def parse_sequence(data: bytes) -> List[SequenceCommand]:
    """An orderlist's commands; bytes stored after its terminator are kept raw."""
    commands, size = GRAMMAR.parse_prefix(data, "sequence")
    return commands + [RawSequenceByte(b) for b in data[size:]]


def parse_pattern(packed: bytes, length: int) -> Pattern:
    """The pattern a NOP-packed body (``$FF`` terminator implicit) holds."""
    return Pattern(rows=GRAMMAR.parse_exact(packed + b"\xff", "pattern"), length=length)


def parse_instrument(body: bytes, name: bytes, exact: bool = False) -> Instrument:
    """The instrument a body (filter-table ``$FF`` implicit) holds.

    ``exact`` also requires its header pointers to be where its tables start, as the
    editor writes them; a ``.sid`` export may patch bytes after the editor's layout.
    """
    ins = replace(GRAMMAR.parse_exact(body + b"\xff", "instrument"), name=bytes(name))
    if exact and ins.encode() != body:
        raise SWMFormatError(
            f"instrument table pointers {body[0x0A]}/{body[0x0B]} are not where its tables start"
        )
    return ins


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
