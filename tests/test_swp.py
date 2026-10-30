"""Tests for the standalone packed-tune (``SWP`` / ``SWP1``) reader.

``flashitback.swp`` is the SID-Maker-SWP export of the native
``examples/flashitback.swm``; both are SHA-256 verified from the tarball, so
the packed model must match and render as the native model.
"""

from __future__ import annotations

import pytest

from pysidwizard import (
    SIDFormatError,
    SidWizardSidParser,
    SWMPlayer,
    parse_swp,
    read_swp,
    sidmaker,
)
from pysidwizard.reader import parse_swm
from tests._swm_cache import swm_path, swp_path

_TUNE = "flashitback.swp"


def _instrument_fields(ins):
    """Decodable instrument fields (name is an editor-only leftover)."""
    return (
        ins.control,
        ins.attack,
        ins.decay,
        ins.sustain,
        ins.release,
        ins.first_waveform,
        ins.wf_table,
        ins.pw_table,
        ins.filter_table,
        ins.default_chord,
    )


def _row_key(row):
    return (row.note, row.instrument, row.fx, row.fx_value)


def test_parse_swp_decodes_prg_wrapped_flashitback():
    swm = parse_swp(swp_path(_TUNE).read_bytes())
    assert swm.subtune_count == 1
    assert swm.subtune_tempos == [(0x86, 0x84)]
    assert swm.chord_table == bytes.fromhex("0703007E0704007E0003070A7E")
    assert swm.tempo_table == b""
    assert len(swm.patterns) > 0
    assert len(swm.instruments) > 0
    # The real author, from the SWP1 block (not a template default).
    assert swm.author_str() == "HERMIT: FLASH IT BACK"
    # The 2-byte PRG prefix sets the load address.
    assert swm.load_address == 0x1CE6


def test_parse_swp_bare_matches_prg():
    data = swp_path(_TUNE).read_bytes()
    prg = parse_swp(data)
    bare = parse_swp(data[2:])
    for field in ("chord_table", "tempo_table", "author", "subtune_tempos", "subtune_count"):
        assert getattr(bare, field) == getattr(prg, field), field
    assert bare.load_address == 0
    assert len(bare.patterns) == len(prg.patterns)
    assert len(bare.instruments) == len(prg.instruments)


def test_parse_swp_rejects_non_swp():
    for payload in (b"NOT A SWP" + b"\x00" * 60, b"", b"\x00\x00" + b"SWP1" + b"\x00" * 20):
        with pytest.raises(SIDFormatError):
            parse_swp(payload)


def test_parser_routes_swp_and_rejects_bad_sid():
    parser = SidWizardSidParser()
    data = swp_path(_TUNE).read_bytes()
    # A standalone SWP1 tune is parsed by the shared parser, PRG and bare alike.
    assert parser.parse(data).chord_table == parse_swp(data).chord_table
    assert parser.parse(data[2:]).chord_table == parse_swp(data[2:]).chord_table
    with pytest.raises(SIDFormatError):
        parser.parse(b"PSID" + b"\x00" * 8)


def test_read_swp_returns_prg_model():
    dest = swp_path(_TUNE)
    assert read_swp(dest).load_address == 0x1CE6
    assert read_swp(dest).chord_table == parse_swp(dest.read_bytes()).chord_table


def test_swp_model_matches_native_swm():
    """The packed model equals the native SWM for all decodable content."""
    native = parse_swm(swm_path("flashitback").read_bytes())
    packed = parse_swp(swp_path(_TUNE).read_bytes())

    assert packed.subtune_count == native.subtune_count
    assert packed.subtune_tempos == native.subtune_tempos
    assert packed.chord_table == native.chord_table
    assert packed.tempo_table == native.tempo_table
    assert packed.sequences == native.sequences
    # Patterns: same count and row content (editor display length is not recovered).
    assert len(packed.patterns) == len(native.patterns)
    for a, b in zip(packed.patterns, native.patterns, strict=False):
        assert [_row_key(r) for r in a.rows] == [_row_key(r) for r in b.rows]
    # Instruments: same count and decodable fields (names are editor-only).
    assert len(packed.instruments) == len(native.instruments)
    for a, b in zip(packed.instruments, native.instruments, strict=False):
        assert _instrument_fields(a) == _instrument_fields(b)
    # The author is preserved, byte-identical to the SWM's here.
    assert packed.author == native.author
    assert packed.author_str() == "HERMIT: FLASH IT BACK"


def test_swp_renders_identically_to_native_swm():
    """3000 frames of SID register output match the native module."""
    native = parse_swm(swm_path("flashitback").read_bytes())
    packed = parse_swp(swp_path(_TUNE).read_bytes())
    native_frames = [list(f) for f in SWMPlayer(native).iter_frames(3000)]
    packed_frames = [list(f) for f in SWMPlayer(packed).iter_frames(3000)]
    assert packed_frames == native_frames


@pytest.mark.parametrize("version", ["1.7", None])
def test_parse_swp_records_the_sid_maker_release(version):
    swm = read_swp(swp_path(_TUNE), player_version=version)
    assert swm.player_version == (version or sidmaker.LATEST)


def test_parse_swp_rejects_an_unshipped_sid_maker_release():
    with pytest.raises(ValueError, match="no SID-Maker"):
        parse_swp(swp_path(_TUNE).read_bytes(), player_version="0.1")
