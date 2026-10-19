"""Tests for parsing SWM files."""

from __future__ import annotations

from pathlib import Path

import pytest

from pysidwizard import (
    SWMFormatError,
    parse_swm,
    read_swm,
)
from pysidwizard.constants import SWM_MAGIC, subtunes_for
from pysidwizard.reader import _detect_layout


def test_detect_layout_recognises_bare_swm():
    payload = SWM_MAGIC + b"\x00" * 60
    offset, addr, fmt, p00_name = _detect_layout(payload)
    assert offset == 0
    assert addr is None
    assert fmt.sid_amount == 1
    assert p00_name is None


def test_detect_layout_recognises_prg_wrapped_swm():
    payload = b"\xf8\x1f" + SWM_MAGIC + b"\x00" * 60
    offset, addr, fmt, p00_name = _detect_layout(payload)
    assert offset == 2
    assert addr == 0x1FF8
    assert fmt.sid_amount == 1
    assert p00_name is None


def test_detect_layout_rejects_unknown_format():
    with pytest.raises(SWMFormatError):
        _detect_layout(b"NOT A SWM" + b"\x00" * 60)


def test_detect_layout_rejects_truncated_input():
    with pytest.raises(SWMFormatError):
        _detect_layout(b"")


def test_subtunes_for_handles_empty_and_partial_groups():
    assert subtunes_for(0, 3) == 1
    assert subtunes_for(3, 3) == 1
    assert subtunes_for(4, 3) == 2
    assert subtunes_for(6, 3) == 2
    assert subtunes_for(7, 3) == 3


def test_parse_swm_rejects_short_header():
    with pytest.raises(SWMFormatError):
        parse_swm(SWM_MAGIC + b"\x00" * 5)


def test_read_swm_returns_object_with_known_fields(sample_path: Path):
    swm = read_swm(sample_path)
    # Every sample in the SID-Wizard distribution is a PRG-wrapped SWM1 with
    # three sequences (one subtune × three SID channels) and a non-empty
    # author string. (Different samples may have been saved at different
    # PRG load addresses, so just assert that a 16-bit one was recovered.)
    assert swm.load_address is not None
    assert 0 <= swm.load_address <= 0xFFFF
    assert swm.sequence_count == 3
    assert swm.subtune_count == 1
    assert swm.author_str() != ""
    assert len(swm.instruments) > 0
    assert len(swm.patterns) > 0


def test_read_swm_decodes_recognisable_metadata():
    # 'flashitback.swm' is by Hermit and has well-known header values.
    from tests._swm_cache import swm_path

    swm = read_swm(swm_path("flashitback"))
    assert swm.author_str().lower().startswith("hermit")
    # The header recorded by SID-Wizard for this file.
    assert swm.frame_speed == 1
    assert swm.highlight == 4
    assert swm.default_pattern_length == 0x40


def test_read_swm_instruments_have_eight_byte_names(sample_path: Path):
    swm = read_swm(sample_path)
    for ins in swm.instruments:
        assert len(ins.name) == 8


def test_read_swm_subtune_tempo_count_matches_subtune_count(sample_path: Path):
    swm = read_swm(sample_path)
    assert len(swm.subtune_tempos) == swm.subtune_count
    # Every value must fit in a byte (header is byte-addressed).
    for left, right in swm.subtune_tempos:
        assert 0 <= left <= 0xFF
        assert 0 <= right <= 0xFF


def test_read_swm_pattern_rows_re_encode_to_size_byte_minus_one(
    sample_path: Path,
):
    """Every row of every sample re-encodes to ``size - 1`` body bytes."""
    swm = read_swm(sample_path)
    for ptn in swm.patterns:
        if ptn.size_override == 0:
            assert ptn.rows == []
            continue
        # The size byte stored on disk equals the *unpacked* pattern body
        # length plus one (for the implicit 0xFF terminator), so when we
        # re-encode the rows we should reproduce exactly that body.
        assert len(ptn.encode_unpacked()) + 1 == (
            ptn.size_override if ptn.size_override is not None else len(ptn.encode_unpacked()) + 1
        )


def test_parse_swm_truncated_pattern_data_raises():
    # Build a minimal valid header that claims one large pattern, then
    # truncate the body. The reader must complain rather than silently
    # under-consuming.
    header = bytearray(64)
    header[0:4] = SWM_MAGIC
    header[0x0C] = 0  # zero sequences
    header[0x0D] = 1  # one pattern claimed
    header[0x0E] = 0  # zero instruments
    header[0x0F] = 0  # no chord table
    header[0x10] = 0  # no tempo table
    # Body: pattern claims size 250 (huge) with a length byte, but only
    # 1 packed byte is provided.
    body = bytes([0x00, 250, 32])  # one body byte, size=250, length=32
    # Subtune-tempo pair (1 subtune by default for zero sequences).
    body += bytes([0x80, 0x80])
    with pytest.raises(SWMFormatError):
        parse_swm(bytes(header) + body)
