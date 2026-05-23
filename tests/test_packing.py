"""Tests for the NOP-packing helpers and the structured Pattern model."""

import pytest

from pysidwizard import Pattern, Row, pack_pattern, unpack_pattern
from pysidwizard.constants import PACKED_MAX, PACKED_MIN


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x05",
        b"\x05\x06\x07",
        b"\x00",
        b"\x00\x00",
        b"\x00\x00\x00",
        b"\x00" * 9,
        b"\x00" * 10,
        b"\x00" * 19,
        b"\x00" * 20,
        b"\x05\x00\x00\x06",
        b"\x05\x00\x00\x00\x00\x00\x00\x06",
        b"\x00\x00\x05\x00\x00\x00\x00\x06\x00\x00\x00\x00\x00",
    ],
)
def test_pack_unpack_round_trip(data: bytes):
    assert unpack_pattern(pack_pattern(data)) == data


def test_pack_single_zero_emits_literal():
    assert pack_pattern(b"\x00") == b"\x00"


def test_pack_two_zeros_emits_two_literal_zeros():
    # Two consecutive zeros yield no compression: the packer prefers two
    # plain 0x00s over a packed byte that would represent zero extra zeros.
    assert pack_pattern(b"\x00\x00") == b"\x00\x00"


def test_pack_three_zeros_uses_one_packed_byte():
    assert pack_pattern(b"\x00\x00\x00") == bytes([0x00, PACKED_MIN])


def test_pack_ten_zeros_uses_one_max_packed_byte():
    assert pack_pattern(b"\x00" * 10) == bytes([0x00, PACKED_MAX])


def test_pack_more_than_ten_zeros_splits_into_multiple_markers():
    assert pack_pattern(b"\x00" * 11) == bytes([0x00, PACKED_MAX, 0x00])


def test_pack_long_zero_run_chains_packed_markers():
    assert pack_pattern(b"\x00" * 19) == bytes([0x00, PACKED_MAX, PACKED_MAX])


def test_unpack_treats_packed_marker_after_nonzero_as_literal():
    # 0x70 must not be decoded as a marker if preceded by a non-zero byte.
    assert unpack_pattern(b"\x05\x70") == b"\x05\x70"


def test_unpack_treats_first_byte_as_literal_even_in_packed_range():
    assert unpack_pattern(b"\x70") == b"\x70"


def test_pattern_round_trips_through_rows_only():
    """A Pattern with no overrides serialises and parses back identically."""
    ptn = Pattern(
        rows=[
            Row(note=0x31, instrument=1),
            Row(),
            Row(),
            Row(note=0x7E),  # gate-off
        ],
        length=4,
    )
    unpacked = ptn.encode_unpacked()
    rebuilt = Pattern.decode(unpacked, length=ptn.length or 0)
    assert [(r.note, r.instrument, r.fx, r.fx_value) for r in rebuilt.rows] == [
        (r.note, r.instrument, r.fx, r.fx_value) for r in ptn.rows
    ]


def test_pattern_size_override_round_trips_empty_slot():
    # ``size_override=0`` represents an empty reserved pattern slot whose
    # on-disk size byte is zero (no body, no implicit 0xFF terminator).
    ptn = Pattern(rows=[], length=0, size_override=0)
    assert ptn.size_override == 0
    assert ptn.encode_unpacked() == b""
