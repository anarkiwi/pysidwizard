"""Sanity tests for the documented SWM format constants."""

import pytest

from pysidwizard import Waveform, attack_decay, straight_tempo, sustain_release
from pysidwizard import constants as c


def test_header_size_is_64_bytes():
    assert c.TUNE_HEADER_SIZE == 64
    assert c.AUTHOR_POS + c.AUTHOR_LEN == c.TUNE_HEADER_SIZE


def test_magic_is_swm1():
    assert c.SWM_MAGIC == b"SWM1"
    assert len(c.SWM_MAGIC) == 4


def test_packed_nop_range():
    # PACKED_MIN/MAX cover eight markers; the spec says a marker plus its
    # leading literal zero represents 2..9 zeros total.
    assert c.PACKED_MAX - c.PACKED_MIN + 1 == 8


def test_header_field_ordering():
    # The amount fields must appear in the documented contiguous block, in
    # order; downstream tools rely on this layout.
    assert (
        c.SEQUENCE_AMOUNT_POS
        < c.PATTERN_AMOUNT_POS
        < c.INSTRUMENT_AMOUNT_POS
        < c.CHORD_LENGTH_POS
        < c.TEMPO_LENGTH_POS
    )


def test_default_load_address_is_documented_value():
    # SID-Wizard's exporter writes $1FF8 as the PRG load address.
    assert c.DEFAULT_LOAD_ADDRESS == 0x1FF8


def test_instrument_layout_offsets_fit_within_header():
    # Every named field of the fixed instrument header must live inside
    # those 16 bytes; the WF-table pointer marks the boundary just past it.
    fixed_names = [
        n
        for n in dir(c)
        if n.startswith("INST_") and n.endswith("_POS") and n != "INST_WF_TABLE_POS"
    ]
    for name in fixed_names:
        assert 0 <= getattr(c, name) < c.INST_HEADER_SIZE, name
    assert c.INST_WF_TABLE_POS == c.INST_HEADER_SIZE


def test_waveform_flags_match_spec_bits():
    # The four SWM-encoded SID waveforms occupy the low nibble of a byte.
    assert int(Waveform.TRIANGLE) == 0x01
    assert int(Waveform.SAWTOOTH) == 0x02
    assert int(Waveform.PULSE) == 0x04
    assert int(Waveform.NOISE) == 0x08
    # Flags compose with bitwise OR.
    assert int(Waveform.TRIANGLE | Waveform.PULSE) == 0x05


@pytest.mark.parametrize(
    "hi,lo,expected",
    [
        (0x0, 0x0, 0x00),
        (0x0, 0xF, 0x0F),
        (0xF, 0x0, 0xF0),
        (0xA, 0x5, 0xA5),
    ],
)
def test_attack_decay_and_sustain_release_pack_nibbles(hi, lo, expected):
    assert attack_decay(hi, lo) == expected
    assert sustain_release(hi, lo) == expected


@pytest.mark.parametrize("bad_hi,bad_lo", [(-1, 0), (0, -1), (16, 0), (0, 16)])
def test_attack_decay_rejects_out_of_range_nibbles(bad_hi, bad_lo):
    with pytest.raises(ValueError):
        attack_decay(bad_hi, bad_lo)
    with pytest.raises(ValueError):
        sustain_release(bad_hi, bad_lo)


def test_straight_tempo_sets_high_bit_on_value():
    assert straight_tempo(6) == 0x86
    assert straight_tempo(1) == 0x81
    assert straight_tempo(0x7F) == 0xFF


@pytest.mark.parametrize("bad", [0, -1, 128, 256])
def test_straight_tempo_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        straight_tempo(bad)


def test_sequence_commands_are_top_bit_set_and_distinct():
    # All sequence-level commands have bit 7 set so they cannot collide
    # with the pattern-reference numeric range (0x00..0x7F).
    for value in (
        c.SEQUENCE_END,
        c.SEQUENCE_END_WITH_LOOP,
        c.SEQUENCE_TRANSPOSE_BASE,
        c.SEQUENCE_TEMPO_BASE,
    ):
        assert value & 0x80
    assert c.SEQUENCE_END != c.SEQUENCE_END_WITH_LOOP
