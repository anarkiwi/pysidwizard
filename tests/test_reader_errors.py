"""Truncation / corruption tests for :func:`parse_swm` error paths."""

from __future__ import annotations

import pytest

from pysidwizard import (
    End,
    Pattern,
    SWMFile,
    SWMFormatError,
    build_swm,
    parse_swm,
)
from pysidwizard.constants import (
    AUTHOR_LEN,
    AUTHOR_POS,
    CHORD_LENGTH_POS,
    INSTRUMENT_AMOUNT_POS,
    PATTERN_AMOUNT_POS,
    SEQUENCE_AMOUNT_POS,
    SWM_MAGIC,
    TEMPO_LENGTH_POS,
    TUNE_HEADER_SIZE,
)


def _make_header(**counts) -> bytearray:
    h = bytearray(TUNE_HEADER_SIZE)
    h[0:4] = SWM_MAGIC
    h[0x08:0x0B] = b"\xff\xff\xff"  # mute
    h[SEQUENCE_AMOUNT_POS] = counts.get("sequences", 0)
    h[PATTERN_AMOUNT_POS] = counts.get("patterns", 0)
    h[INSTRUMENT_AMOUNT_POS] = counts.get("instruments", 0)
    h[CHORD_LENGTH_POS] = counts.get("chord", 0)
    h[TEMPO_LENGTH_POS] = counts.get("tempo", 0)
    h[AUTHOR_POS : AUTHOR_POS + AUTHOR_LEN] = b" " * AUTHOR_LEN
    return h


def test_parse_swm_truncated_subtune_block_raises():
    # zero sequences => 1 subtune => needs 2 trailing bytes
    header = _make_header()
    with pytest.raises(SWMFormatError, match="subtune-tempo"):
        parse_swm(bytes(header))  # no body at all


def test_parse_swm_truncated_tempo_table_raises():
    header = _make_header(tempo=10)
    body = bytes([0x80, 0x80])  # only subtune tempos
    with pytest.raises(SWMFormatError, match="tempo table"):
        parse_swm(bytes(header) + body)


def test_parse_swm_truncated_chord_table_raises():
    header = _make_header(chord=10)
    body = bytes([0x80, 0x80])
    with pytest.raises(SWMFormatError, match="chord table"):
        parse_swm(bytes(header) + body)


def test_parse_swm_truncated_instrument_name_raises():
    header = _make_header(instruments=1)
    body = bytes([0x80, 0x80])  # not enough for an 8-byte name
    with pytest.raises(SWMFormatError, match="instrument name"):
        parse_swm(bytes(header) + body)


def test_parse_swm_truncated_instrument_size_raises():
    header = _make_header(instruments=1)
    body = b"NAME    " + bytes([0x80, 0x80])  # name fits, no size byte
    with pytest.raises(SWMFormatError, match="instrument size byte"):
        parse_swm(bytes(header) + body)


def test_parse_swm_truncated_instrument_body_raises():
    header = _make_header(instruments=1)
    # Size byte claims 50 bytes of body but only one is provided.
    body = b"\x01" + bytes([50]) + b"NAME    " + bytes([0x80, 0x80])
    with pytest.raises(SWMFormatError, match="instrument body"):
        parse_swm(bytes(header) + body)


def test_parse_swm_truncated_pattern_header_raises():
    header = _make_header(patterns=1)
    body = bytes([0x80, 0x80])  # nothing left for the pattern's header bytes
    with pytest.raises(SWMFormatError, match="pattern header"):
        parse_swm(bytes(header) + body)


def test_parse_swm_truncated_sequence_size_raises():
    header = _make_header(sequences=3)
    body = bytes([0x80, 0x80])  # subtune tempos but no sequence size bytes
    with pytest.raises(SWMFormatError, match="sequence size byte"):
        parse_swm(bytes(header) + body)


def test_parse_swm_truncated_sequence_body_raises():
    header = _make_header(sequences=1)
    # Size byte claims 5 bytes of sequence body but only 0 are present.
    body = bytes([5, 0x80, 0x80])
    with pytest.raises(SWMFormatError, match="sequence body"):
        parse_swm(bytes(header) + body)


def test_parse_swm_extra_unconsumed_bytes_raises():
    swm = SWMFile(
        sequences=[[End()]] * 3,
        patterns=[],
        instruments=[],
        subtune_tempos=[(0x80, 0x80)],
    )
    payload = build_swm(swm)
    header_end = 2 + TUNE_HEADER_SIZE  # PRG load address + header
    spliced = payload[:header_end] + b"\x00" + payload[header_end:]
    with pytest.raises(SWMFormatError, match="trailing/leading data mismatch"):
        parse_swm(spliced)


def test_parse_swm_size_zero_pattern_round_trips():
    """A reserved empty pattern slot (size_override=0) round-trips."""
    swm = SWMFile(
        sequences=[[End()]] * 3,
        patterns=[Pattern(rows=[], length=0, size_override=0)],
        instruments=[],
        subtune_tempos=[(0x80, 0x80)],
    )
    rebuilt = parse_swm(build_swm(swm))
    assert len(rebuilt.patterns) == 1
    assert rebuilt.patterns[0].size_override == 0
    assert rebuilt.patterns[0].rows == []


def test_parse_swm_pattern_body_truncated_inside_loop_raises():
    # Build a header claiming one pattern whose size byte indicates a body
    # that extends past the start of the SWM body.
    header = _make_header(patterns=1)
    # Pattern: length=32, size=10 but only 1 packed body byte present.
    body = bytes([0x05, 10, 32, 0x80, 0x80])
    with pytest.raises(SWMFormatError, match="pattern body"):
        parse_swm(bytes(header) + body)
