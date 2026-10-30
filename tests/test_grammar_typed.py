"""The song grammar types every column and table row as the player decodes it."""

import random
import re

import pytest

from pysidwizard import Instrument, Pattern, Row, parse_swm, unpack_pattern
from pysidwizard.constants import (
    CHORD_LOOP,
    GATE_OFF_FX,
    GATE_ON_FX,
    INSTRUMENT_MAX,
    LEGATO_INSFX,
    NOTE_MAX,
    PATTERN_NEXT_COLUMN_FLAG,
    PORTAMENTO_FX,
    RING_OFF_FX,
    RING_ON_FX,
    SMALL_FX_INST_MIN,
    SMALL_FX_MIN,
    SYNC_OFF_FX,
    SYNC_ON_FX,
    TABLE_END,
    TABLE_JUMP,
    VIBRATO_FX,
)
from pysidwizard.formats import FORMAT
from pysidwizard.grammar import GRAMMAR, RULES, TABLE_RULES
from pysidwizard.model import pack_pattern
from pysidwizard.typed import (
    AbsolutePitch,
    BigEffect,
    ChordPitch,
    Column,
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
    VibratoAmplitude,
    WaveRow,
)
from tests._swm_cache import all_swm_paths

NEXT = PATTERN_NEXT_COLUMN_FLAG


def _rows(*packed: int):
    return GRAMMAR.parse_exact(bytes(packed) + bytes([TABLE_END]), "pattern")


def _table(rule: str, *data: int):
    return GRAMMAR.parse_exact(bytes(data) + bytes([TABLE_END]), rule)


NOTE_KINDS = [
    (1, Note),
    (NOTE_MAX, Note),
    (VIBRATO_FX, VibratoAmplitude),
    (PORTAMENTO_FX, Portamento),
    (SYNC_ON_FX, SyncOn),
    (SYNC_OFF_FX, SyncOff),
    (RING_ON_FX, RingOn),
    (RING_OFF_FX, RingOff),
    (GATE_ON_FX, GateOn),
    (GATE_OFF_FX, GateOff),
]


@pytest.mark.parametrize("byte, kind", NOTE_KINDS)
def test_note_column_value_parses_to_its_kind_with_or_without_an_instrument(byte, kind):
    (alone,) = _rows(byte)
    (with_inst,) = _rows(byte | NEXT, 1)
    for row in (alone, with_inst):
        assert type(row.note) is kind and row.note == byte


def test_note_7f_is_a_note_effect_of_no_named_kind():
    (row,) = _rows(GATE_OFF_FX + 1)
    assert type(row.note) is NoteEffect


def test_vibrato_note_carries_its_amplitude_nibble():
    (row,) = _rows(VIBRATO_FX + 9)
    assert row.note.amplitude == 9


@pytest.mark.parametrize(
    "byte, kind",
    [(1, SelectInstrument), (INSTRUMENT_MAX, SelectInstrument), (LEGATO_INSFX, Legato)]
    + [(SMALL_FX_INST_MIN, SmallEffect), (0x7F, SmallEffect)],
)
def test_instrument_column_value_parses_to_its_kind(byte, kind):
    (row,) = _rows(NEXT, byte)
    (fx_row,) = _rows(NEXT, byte | NEXT, SMALL_FX_MIN)
    assert type(row.instrument) is kind and type(fx_row.instrument) is kind
    assert row.note is None and fx_row.fx == SMALL_FX_MIN


def test_small_effect_splits_into_effect_number_and_value_nibbles():
    (row,) = _rows(NEXT, NEXT, 0xA5)
    (inst,) = _rows(NEXT, 0x57)
    assert (row.fx.effect, row.fx.value) == (0xA, 5)
    assert (inst.instrument.effect, inst.instrument.value) == (5, 7)


def test_big_effect_takes_the_value_byte_and_small_effects_none():
    big, small = _rows(NEXT, NEXT, SMALL_FX_MIN - 1, 0x42, NEXT, NEXT, SMALL_FX_MIN)
    assert type(big.fx) is BigEffect and big.fx_value == 0x42
    assert type(small.fx) is SmallEffect and small.fx_value is None


def test_typed_columns_equal_and_encode_as_their_bytes():
    assert Row(note=Note(0x31), instrument=Legato(LEGATO_INSFX)) == Row(0x31, LEGATO_INSFX)
    assert repr(GateOff(GATE_OFF_FX)) == f"GateOff(0x{GATE_OFF_FX:02x})"
    assert Row(note=GateOn(GATE_ON_FX)).encode() == bytes([GATE_ON_FX])


def _random_row(rng: random.Random) -> Row:
    fx = rng.choice([None, rng.randrange(1, 0x100)])
    return Row(
        note=rng.choice([None, rng.randrange(1, GATE_OFF_FX + 1)]),
        instrument=rng.choice([None, rng.randrange(1, 0x80)]),
        fx=fx,
        fx_value=rng.randrange(0x100) if fx is not None and fx < SMALL_FX_MIN else None,
    )


def test_random_patterns_agree_with_the_byte_decoder():
    rng = random.Random(120)
    for _ in range(300):
        rows = [rng.choice([Row(), _random_row(rng)]) for _ in range(rng.randrange(1, 40))]
        packed = pack_pattern(b"".join(row.encode() for row in rows))
        parsed = GRAMMAR.parse_exact(packed + bytes([TABLE_END]), "pattern")
        assert parsed == Pattern.decode(unpack_pattern(packed), len(rows)).rows
        assert all(isinstance(c, Column) for r in parsed for c in (r.note, r.instrument) if c)


@pytest.mark.parametrize(
    "pitch, kind",
    [(0, RelativePitch), (CHORD_LOOP, ChordPitch), (0x80, KeepPitch), (0x81, AbsolutePitch)]
    + [(0xDF, AbsolutePitch), (0xE0, RelativePitch)],
)
def test_wf_row_pitch_column_parses_to_its_kind(pitch, kind):
    (row,) = _table("wf_table", 0x41, pitch, 0)
    assert type(row.pitch) is kind and row.pitch == pitch


def test_wf_rows_by_first_column_and_detune():
    rows = _table("wf_table", 0x0F, 0xFF, TABLE_END, 0x10, 0x8C, 5, TABLE_JUMP, 0x13, 0)
    assert rows == [
        RepeatRow(0x0F, RelativePitch(0xFF)),
        WaveRow(0x10, AbsolutePitch(0x8C), 5),
        TableJump(0x13),
    ]
    assert rows[0].pitch.semitones == -1 and rows[1].pitch.note == 0x0C


def test_jump_row_parses_to_a_jump_with_its_target_in_every_table():
    for rule in TABLE_RULES:
        assert _table(rule, TABLE_JUMP, 0x22, 0x01) == [TableJump(target=0x22, pad=1)]


def test_pulse_and_filter_rows_split_as_the_player_reads_them():
    assert _table("pw_table", 0x10, 0xF0, 3, 0x88, 0x40, 0) == [
        PulseSweep(0x10, -0x10, 3),
        PulseSet(high=8, low=0x40),
    ]
    assert _table("filter_table", 0x7F, 0x02, 0, 0x9C, 0x30, 0x81) == [
        FilterSweep(0x7F, 2),
        FilterSet(band=0x10, resonance=0xC, cutoff=0x30, keytrack=0x81),
    ]


@pytest.mark.parametrize("rule", TABLE_RULES)
def test_random_tables_encode_back_to_their_bytes(rule):
    rng = random.Random(rule)
    for _ in range(200):
        data = b"".join(
            bytes([rng.randrange(TABLE_END), rng.randrange(0x100), rng.randrange(0x100)])
            for _ in range(rng.randrange(8))
        )
        rows, size = GRAMMAR.parse_prefix(data + bytes([TABLE_END]) + data, rule)
        assert size == len(data) + 1
        assert b"".join(row.encode() for row in rows) == data


@pytest.mark.parametrize("tune", sorted(all_swm_paths()))
def test_real_modules_tables_chords_and_tempos_type_and_encode_back(tune):
    swm = parse_swm(all_swm_paths()[tune].read_bytes())
    for ins in swm.instruments:
        for rows, table in (
            (ins.wf_rows, ins.wf_table),
            (ins.pw_rows, ins.pw_table),
            (ins.filter_rows, ins.filter_table),
        ):
            assert b"".join(row.encode() for row in rows) == table
    assert b"".join(chord.encode() for chord in swm.chords) == swm.chord_table
    assert b"".join(p.encode() for p in swm.tempo_programs) == swm.tempo_table


def test_tables_read_from_their_start_to_the_first_terminator():
    ins = Instrument(wf_table=bytes([0x41, 0, 0, TABLE_END, 0x88, 0]), pw_table=b"")
    assert ins.wf_rows == [WaveRow(0x41, RelativePitch(0), 0)]
    assert ins.pw_rows == []


def test_instrument_header_lists_as_named_fields():
    swm = parse_swm(next(iter(all_swm_paths().values())).read_bytes())
    header = next(line for line in FORMAT.listing(swm) if line.strip().startswith("header"))
    assert re.findall(r"([A-Z_]+)=", header)[:3] == ["CONTROL", "HR_AD", "HR_SR"]


def test_no_parser_state_lexes_one_byte_as_two_terminals_except_a_packed_run():
    lark = GRAMMAR.parser(trees=True)
    matches = {
        term.name: {b for b in range(0x100) if re.fullmatch(term.pattern.to_regexp(), chr(b))}
        for term in lark.terminals
    }
    for actions in lark.parser.parser.parser.parse_table.states.values():
        names = sorted(name for name in actions if name in matches and name != "PACK")
        for i, name in enumerate(names):
            for other in names[i + 1 :]:
                assert not matches[name] & matches[other], (name, other)
    assert set(RULES + TABLE_RULES) <= set(lark.options.start)
