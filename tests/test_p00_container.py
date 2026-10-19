"""PC64 .P00-wrapped SWM modules: detection, model equality, byte-exact re-emit (issue #61)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Tuple

import pytest

from pysidwizard import (
    SWMFile,
    SWMFormatError,
    build_swm,
    parse_swm,
    read_swm,
    write_swm,
)
from pysidwizard.constants import P00_HEADER_SIZE, P00_MAGIC, P00_NAME_LEN, SWM_MAGIC
from pysidwizard.reader import _detect_layout


def _wrap_p00(payload: bytes = SWM_MAGIC + b"\x00" * 60, name: bytes = b"QUICKSCORE.SWM") -> bytes:
    """A PRG-wrapped payload under the 26-byte PC64 header, as SWMconvert emits it."""
    return (
        P00_MAGIC
        + name.ljust(P00_NAME_LEN, b"\x00")
        + b"\x00\x00"
        + (0x2031).to_bytes(2, "little")
        + payload
    )


def test_detect_layout_recognises_p00_wrapped_swm():
    offset, addr, fmt, name = _detect_layout(_wrap_p00())
    assert offset == P00_HEADER_SIZE + 2
    assert addr == 0x2031
    assert fmt.sid_amount == 1
    assert name == b"QUICKSCORE.SWM\x00\x00"


def test_detect_layout_bare_and_prg_keep_none_name():
    offset, addr, fmt, name = _detect_layout(SWM_MAGIC + b"\x00" * 60)
    assert (offset, addr, fmt.sid_amount, name) == (0, None, 1, None)
    offset, addr, fmt, name = _detect_layout(b"\xf8\x1f" + SWM_MAGIC + b"\x00" * 60)
    assert (offset, addr, name) == (2, 0x1FF8, None)


def test_detect_layout_rejects_p00_with_non_swm_payload():
    # The .P00-wrapped GM soundset / instrument files in these releases are not SWM modules.
    data = P00_MAGIC + b"DRMNDBSS" + b"\x00" * 8 + b"\x00\x00" + b"\x1a\x0f\x00\x0b" + b"\x00" * 10
    with pytest.raises(SWMFormatError, match="no SWM1/SWMS/SWT/SWMQ magic at offset 0x1C"):
        _detect_layout(data)


def test_detect_layout_rejects_truncated_p00():
    with pytest.raises(SWMFormatError, match="too short"):
        _detect_layout(P00_MAGIC + b"AB")


def test_detect_layout_rejects_unknown_format():
    with pytest.raises(SWMFormatError, match="does not look like an SWM module"):
        _detect_layout(b"NOT A SWM" + b"\x00" * 60)


def test_p00_payload_is_the_prg_payload(p00_pair_file: Tuple[Path, Path]):
    p00, prg = p00_pair_file
    # The 26-byte VICE header sits directly in front of the ``.prg`` payload.
    assert p00.read_bytes()[P00_HEADER_SIZE:] == prg.read_bytes()


def test_issue_61_read_p00_equals_prg_model_and_round_trips_byte_exact(
    p00_pair_file: Tuple[Path, Path],
):
    p00, prg = p00_pair_file
    p00_bytes, prg_bytes = p00.read_bytes(), prg.read_bytes()
    model_p00 = read_swm(p00)
    model_prg = read_swm(prg)
    assert model_p00 == model_prg
    assert model_p00.p00_name == b"QUICKSCORE.SWM\x00\x00"
    assert model_prg.p00_name == b""
    assert build_swm(model_p00, container="p00") == p00_bytes
    assert build_swm(model_p00) == prg_bytes


def test_parse_swm_p00_round_trips(p00_pair_file: Tuple[Path, Path]):
    p00, _ = p00_pair_file
    data = p00.read_bytes()
    model = parse_swm(data)
    assert build_swm(model, container="p00") == data
    assert parse_swm(build_swm(model, container="p00")) == model


def test_build_swm_p00_header_layout(p00_pair_file: Tuple[Path, Path]):
    _, prg = p00_pair_file
    swm = read_swm(prg)
    swm.p00_name = b"AB"
    out = build_swm(swm, container="p00")
    assert out[:8] == P00_MAGIC
    assert out[8 : 8 + P00_NAME_LEN] == b"AB" + b"\x00" * 14
    assert out[24:26] == b"\x00\x00"
    assert out[26:28] == swm.load_address.to_bytes(2, "little")
    assert out[28:32] == SWM_MAGIC
    assert out[P00_HEADER_SIZE:] == build_swm(swm)


def test_build_swm_p00_empty_name_is_zero_padded(p00_pair_file: Tuple[Path, Path]):
    _, prg = p00_pair_file
    swm = read_swm(prg)
    out = build_swm(swm, container="p00")
    assert out[8:24] == b"\x00" * 16
    assert parse_swm(out) == swm


def test_build_swm_p00_rejects_oversized_name(p00_pair_file: Tuple[Path, Path]):
    _, prg = p00_pair_file
    swm = read_swm(prg)
    swm.p00_name = b"x" * 17
    with pytest.raises(SWMFormatError, match="p00_name"):
        build_swm(swm, container="p00")


def test_build_swm_rejects_unknown_container(p00_pair_file: Tuple[Path, Path]):
    _, prg = p00_pair_file
    swm = read_swm(prg)
    with pytest.raises(SWMFormatError, match="container"):
        build_swm(swm, container="s00")


def test_write_swm_p00_container(tmp_path: Path, p00_pair_file: Tuple[Path, Path]):
    p00, _ = p00_pair_file
    swm = read_swm(p00)
    path = tmp_path / "tune.swm.P00"
    write_swm(swm, path, container="p00")
    assert path.read_bytes() == p00.read_bytes()


def test_p00_name_is_not_part_of_model_equality(p00_pair_file: Tuple[Path, Path]):
    p00, prg = p00_pair_file
    wrapped = read_swm(p00)
    bare = read_swm(prg)
    assert wrapped == dataclasses.replace(bare, p00_name=wrapped.p00_name)


def test_p00_name_defaults_to_empty():
    assert SWMFile().p00_name == b""
