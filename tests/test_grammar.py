"""The song grammar (``sidwizard.lark``) accepts what the player reads and rejects the rest."""

import pytest

from pysidwizard import (
    End,
    Instrument,
    Loop,
    MainVolume,
    PlayPattern,
    RawSequenceByte,
    Row,
    SubtuneJump,
    TempoOverride,
    Transpose,
    parse_swm,
)
from pysidwizard.errors import SWMFormatError
from pysidwizard.formats import FORMAT
from pysidwizard.grammar import GRAMMAR, parse_instrument, parse_pattern, parse_sequence
from tests._swm_cache import all_swm_paths


def _rows(*packed: int):
    return GRAMMAR.parse_exact(bytes(packed) + b"\xff", "pattern")


def test_sequence_commands_by_byte_band():
    data = bytes([0x05, 0x7F, 0x90, 0x8F, 0xA3, 0xB0, 0xEF, 0xF5, 0xFE])
    assert parse_sequence(data) == [
        PlayPattern(5),
        PlayPattern(0x7F),
        Transpose(0),
        Transpose(-1),
        MainVolume(3),
        TempoOverride(0),
        TempoOverride(0x3F),
        RawSequenceByte(0xF5),
        End(),
    ]


def test_sequence_loop_position_below_80_and_subtune_jump_at_or_above():
    assert parse_sequence(b"\x01\xff\x00") == [PlayPattern(1), Loop(0)]
    assert parse_sequence(b"\xff\x7f") == [Loop(0x7F)]
    assert parse_sequence(b"\xff\x82") == [SubtuneJump(0x82)]


def test_sequence_bytes_after_the_terminator_are_never_played():
    assert parse_sequence(b"\x01\xfe\x02\xff") == [
        PlayPattern(1),
        End(),
        RawSequenceByte(2),
        RawSequenceByte(0xFF),
    ]
    assert GRAMMAR.parse_prefix(b"\x01\xfe\x02\xff", "sequence").size == 2


@pytest.mark.parametrize("data", [b"", b"\x01\x02", b"\x01\xff"])
def test_unterminated_sequence_is_a_parse_error(data):
    with pytest.raises(SWMFormatError, match="sequence"):
        parse_sequence(data)


def test_pattern_row_columns():
    assert _rows(0x31, 0x00, 0xB1, 0x05, 0xB1, 0x85, 0x20, 0x81, 0x80, 0x03, 0x07) == [
        Row(note=0x31),
        Row(),
        Row(note=0x31, instrument=5),
        Row(note=0x31, instrument=5, fx=0x20),
        Row(note=1, instrument=None, fx=3, fx_value=7),
    ]


def test_pattern_zero_run_marker_expands_to_extra_empty_rows():
    assert _rows(0x00, 0x70) == [Row()] * 3
    assert _rows(0x00, 0x77) == [Row()] * 10
    assert _rows(0x00, 0x70, 0x71) == [Row()] * 6


def test_pattern_zero_fx_value_can_be_followed_by_a_marker():
    assert (
        _rows(0x81, 0x81, 0x01, 0x00, 0x71)
        == [Row(note=1, instrument=1, fx=1, fx_value=0)] + [Row()] * 3
    )


def test_pattern_70_to_77_after_a_non_zero_byte_is_a_note():
    assert _rows(0x71, 0x00) == [Row(note=0x71), Row()]


def test_pattern_70_to_77_after_another_one_is_a_marker():
    assert _rows(0x72, 0x72) == [Row(note=0x72)] + [Row()] * 4


def test_first_pattern_byte_is_never_a_marker():
    assert _rows(0x70) == [Row(note=0x70)]


@pytest.mark.parametrize("packed", [b"\x81", b"\x81\x81", b"\x81\x81\x01"])
def test_pattern_cut_short_inside_a_row_is_a_parse_error(packed):
    with pytest.raises(SWMFormatError):
        parse_pattern(packed, 1)


def _instrument(wf=b"", pw=b"", filt=b"", **kw):
    return Instrument(wf_table=wf, pw_table=pw, filter_table=filt, **kw)


def test_instrument_tables_are_three_byte_rows_and_a_jump_row_is_three_bytes():
    ins = _instrument(wf=b"\x41\x7f\x00\xfe\x00\x00", pw=b"\x88\x00\x00", filt=b"\x94\x20\x00")
    assert parse_instrument(ins.encode(), b"NAME") == _instrument(
        wf=ins.wf_table, pw=ins.pw_table, filt=ins.filter_table, name=b"NAME"
    )


@pytest.mark.parametrize("wf", [b"\x41\x7f", b"\x41\x7f\x00\xfe\x10"])
def test_instrument_table_with_a_short_row_is_a_parse_error(wf):
    with pytest.raises(SWMFormatError):
        parse_instrument(_instrument(wf=wf).encode(), b"")


def test_instrument_pointers_must_match_the_table_layout():
    body = bytearray(_instrument(wf=b"\x41\x00\x00", pw=b"\x88\x00\x00").encode())
    body[0x0A] += 3
    with pytest.raises(SWMFormatError, match="pointers"):
        parse_instrument(bytes(body), b"", exact=True)
    assert parse_instrument(bytes(body), b"").wf_table == bytes(body[0x10 : body[0x0A] - 1])


def test_chord_table_is_chords_each_ending_7e_or_7f():
    assert (
        GRAMMAR.parse_exact(b"\x00\x04\x07\x7e\x00\x03\x7f", "chords")
        == b"\x00\x04\x07\x7e\x00\x03\x7f"
    )
    with pytest.raises(SWMFormatError):
        GRAMMAR.parse_exact(b"\x00\x04\x07", "chords")


def test_tempo_table_is_programs_each_closed_by_a_byte_with_bit_7():
    assert GRAMMAR.parse_exact(b"\x03\x04\x85\x82", "tempos") == b"\x03\x04\x85\x82"
    with pytest.raises(SWMFormatError):
        GRAMMAR.parse_exact(b"\x03\x04", "tempos")


@pytest.mark.parametrize("tune", sorted(all_swm_paths()))
def test_every_stream_of_a_real_module_reads_back_as_the_editor_wrote_it(tune):
    swm = parse_swm(all_swm_paths()[tune].read_bytes())
    for name, model, rule in FORMAT.streams(swm):
        data = model.to_bytes()
        assert GRAMMAR.parse_prefix(data, rule).size == len(data), name


def test_format_lists_the_parse_tree_of_each_stream():
    swm = parse_swm(next(iter(all_swm_paths().values())).read_bytes())
    lines = list(FORMAT.listing(swm))
    assert lines[0] == "sequence 0:"
    assert any(line.strip() == "pattern" for line in lines)
