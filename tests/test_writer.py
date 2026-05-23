"""Tests for serialising SWM files via :func:`build_swm` / :func:`write_swm`."""

from __future__ import annotations

import pytest

from pysidwizard import (
    End,
    Instrument,
    Pattern,
    PlayPattern,
    Row,
    SWMFile,
    SWMFormatError,
    build_swm,
    parse_swm,
    write_swm,
)
from pysidwizard.constants import DEFAULT_LOAD_ADDRESS, SWM_MAGIC
from pysidwizard.model import SequenceCommand


def _minimal_swm(**overrides) -> SWMFile:
    """A smallest-possible valid module: one row, one instrument."""
    return SWMFile(
        sequences=[[PlayPattern(1), End()]] * 3,
        patterns=[Pattern(rows=[Row(note=0x31, instrument=1), Row(note=0x7E)], length=2)],
        instruments=[
            Instrument(
                name=b"TEST    ",
                sustain=0xF,
                first_waveform=0x04,
            )
        ],
        chord_table=b"",
        tempo_table=b"",
        subtune_tempos=[(0x80, 0x80)],
        **overrides,
    )


def test_build_swm_round_trips_through_parse():
    original = _minimal_swm()
    bytes_out = build_swm(original)
    parsed = parse_swm(bytes_out)
    assert parsed.sequence_count == original.sequence_count
    assert len(parsed.patterns) == len(original.patterns)
    assert parsed.patterns[0].rows == original.patterns[0].rows
    assert parsed.instruments[0].name == original.instruments[0].name


def test_build_swm_emits_prg_load_address_when_set():
    swm = _minimal_swm()
    assert swm.load_address == DEFAULT_LOAD_ADDRESS
    payload = build_swm(swm)
    assert payload[0:2] == DEFAULT_LOAD_ADDRESS.to_bytes(2, "little")
    assert payload[2:6] == SWM_MAGIC


def test_build_swm_can_emit_bare_module_without_prg_wrapper():
    swm = _minimal_swm(load_address=None)
    payload = build_swm(swm)
    assert payload[0:4] == SWM_MAGIC


def test_build_swm_rejects_too_many_sequences():
    swm = _minimal_swm()
    swm.sequences = [[End()]] * 256
    with pytest.raises(SWMFormatError, match="sequences"):
        build_swm(swm)


def test_build_swm_rejects_oversized_sequence():
    swm = _minimal_swm()
    # 256 raw bytes worth of pattern references then End() — overflows the
    # one-byte size field for sequence 0.
    oversized: list[SequenceCommand] = list([PlayPattern(1)] * 256)
    oversized.append(End())
    swm.sequences = [oversized, [End()], [End()]]
    with pytest.raises(SWMFormatError, match="sequence 0"):
        build_swm(swm)


def test_build_swm_rejects_sequence_without_terminator():
    swm = _minimal_swm()
    swm.sequences[0] = [PlayPattern(1)]  # type: ignore[list-item]  # no End / Loop
    with pytest.raises(SWMFormatError, match="End or Loop"):
        build_swm(swm)


def test_build_swm_rejects_play_pattern_out_of_range():
    swm = _minimal_swm()
    swm.sequences[0] = [PlayPattern(99), End()]
    with pytest.raises(SWMFormatError, match="references pattern 99"):
        build_swm(swm)


def test_build_swm_rejects_pattern_row_with_unknown_instrument():
    swm = _minimal_swm()
    swm.patterns[0].rows[0] = Row(note=0x31, instrument=42)
    with pytest.raises(SWMFormatError, match="instrument 42"):
        build_swm(swm)


def test_build_swm_allows_instrument_column_fx_without_instrument_def():
    # Volume / chord-set / legato FX in the instrument column (>= 0x3F)
    # are *not* instrument references and should not trigger the
    # cross-check.
    swm = _minimal_swm()
    swm.patterns[0].rows[0] = Row(note=0x31, instrument=0x50)  # volume FX
    build_swm(swm)  # must not raise


def test_build_swm_allows_play_pattern_zero_as_empty_placeholder():
    # SID-Wizard reserves pattern 0 as the empty-pattern placeholder, so
    # PlayPattern(0) must validate even though no swm.patterns[0-1] exists.
    swm = _minimal_swm()
    swm.sequences[0] = [PlayPattern(0), PlayPattern(1), End()]
    build_swm(swm)  # must not raise


def test_build_swm_rejects_bad_mute_length():
    swm = _minimal_swm()
    swm.mute = b"\xff\xff"
    with pytest.raises(SWMFormatError, match="mute"):
        build_swm(swm)


def test_build_swm_rejects_bad_reserved_length():
    swm = _minimal_swm()
    swm.reserved = b"\x00"
    with pytest.raises(SWMFormatError, match="reserved"):
        build_swm(swm)


def test_build_swm_rejects_oversized_byte_fields():
    swm = _minimal_swm()
    swm.frame_speed = 999
    with pytest.raises(SWMFormatError, match="frame_speed"):
        build_swm(swm)


def test_build_swm_rejects_oversized_instrument_name():
    swm = _minimal_swm()
    swm.instruments[0].name = b"TOO LONG NAME"
    with pytest.raises(SWMFormatError, match="instrument 0 name"):
        build_swm(swm)


def test_build_swm_rejects_wrong_subtune_count():
    swm = _minimal_swm()
    swm.subtune_tempos = []
    with pytest.raises(SWMFormatError, match="subtune_tempos"):
        build_swm(swm)


def test_build_swm_rejects_oversized_load_address():
    swm = _minimal_swm(load_address=0x10000)
    with pytest.raises(SWMFormatError, match="load_address"):
        build_swm(swm)


def test_write_swm_writes_to_disk(tmp_path):
    swm = _minimal_swm()
    path = tmp_path / "out.swm"
    write_swm(swm, path)
    assert path.read_bytes() == build_swm(swm)


def test_build_swm_pads_short_author_to_header_width():
    swm = _minimal_swm(author=b"AB")
    payload = build_swm(swm)
    header_offset = 2
    author_offset = header_offset + 0x18
    assert payload[author_offset : author_offset + 40] == b"AB" + b"\x00" * 38


def test_build_swm_rejects_oversized_author_field():
    swm = _minimal_swm(author=b"x" * 41)
    with pytest.raises(SWMFormatError, match="author"):
        build_swm(swm)


def test_build_swm_rejects_pattern_with_oversized_size_byte():
    swm = _minimal_swm()
    swm.patterns[0].size_override = 300
    with pytest.raises(SWMFormatError, match="pattern 0 size byte"):
        build_swm(swm)


def test_build_swm_rejects_oversized_instrument_body():
    # ``filter_table`` comes after the pointers, so growing it lets us
    # push past the 255-byte body cap without first hitting the per-
    # pointer overflow check in :meth:`Instrument.encode`.
    swm = _minimal_swm()
    swm.instruments[0].filter_table = b"\x00" * 240
    with pytest.raises(SWMFormatError, match="instrument 0 body"):
        build_swm(swm)


def test_build_swm_rejects_oversized_instrument_table_pointer():
    # A huge wf_table pushes the PW table pointer past one byte; the
    # encoder catches this before we get to the writer.
    swm = _minimal_swm()
    swm.instruments[0].wf_table = b"\x00" * 250
    with pytest.raises(SWMFormatError, match="pw_table_ptr"):
        build_swm(swm)
