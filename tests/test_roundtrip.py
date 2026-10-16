"""End-to-end round-trip tests against real SID-Wizard sample modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from pysidwizard import SWMFormatError, build_swm, parse_swm, read_swm


def test_read_then_build_is_byte_exact(sample_path: Path, sample_bytes: bytes):
    """``build_swm(read_swm(p)) == open(p).read()`` for every real sample."""
    swm = read_swm(sample_path)
    assert build_swm(swm) == sample_bytes


def test_parse_then_build_is_byte_exact(sample_bytes: bytes):
    """The same round-trip from in-memory ``parse_swm``."""
    assert build_swm(parse_swm(sample_bytes)) == sample_bytes


def test_round_trip_preserves_pattern_count(sample_path: Path):
    swm = read_swm(sample_path)
    rebuilt = parse_swm(build_swm(swm))
    assert len(rebuilt.patterns) == len(swm.patterns)


def test_round_trip_preserves_instrument_payloads(sample_path: Path):
    swm = read_swm(sample_path)
    rebuilt = parse_swm(build_swm(swm))
    for a, b in zip(swm.instruments, rebuilt.instruments, strict=True):
        # The full field-by-field equality check (Instrument is a
        # dataclass) catches any drift in fixed-header fields *or* in the
        # variable-length tables.
        assert a == b


def test_round_trip_preserves_chord_and_tempo_tables(sample_path: Path):
    swm = read_swm(sample_path)
    rebuilt = parse_swm(build_swm(swm))
    assert rebuilt.chord_table == swm.chord_table
    assert rebuilt.tempo_table == swm.tempo_table


def test_round_trip_preserves_subtune_tempos(sample_path: Path):
    swm = read_swm(sample_path)
    rebuilt = parse_swm(build_swm(swm))
    assert rebuilt.subtune_tempos == swm.subtune_tempos


def test_oob_instrument_round_trips(oob_sample_bytes: bytes):
    # issue #57: refs above the instrument count must round-trip byte-exactly.
    assert build_swm(parse_swm(oob_sample_bytes)) == oob_sample_bytes


def test_oob_instrument_strict_still_raises(oob_sample_bytes: bytes):
    # the default is lenient; explicit strict validation must still reject them.
    with pytest.raises(SWMFormatError, match="uses instrument"):
        build_swm(parse_swm(oob_sample_bytes), strict=True)
