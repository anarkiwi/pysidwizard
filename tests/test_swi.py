"""Tests for the ``.swi`` single-instrument codec (issue #54).

Unit round-trips and error paths need no network; fixture tests require a
byte-exact round-trip over a SHA-256-pinned spread of the editor's real
``.swi`` files (plus a SWM/``.swi`` shared-instrument cross-check pair).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pysidwizard import (
    SWI_DEFAULT_LOAD_ADDRESS,
    Instrument,
    SWMFile,
    SWMFormatError,
    build_swi,
    insert_instrument,
    parse_swi,
    read_swi,
    read_swm,
    write_swi,
)
from tests._swi_cache import _bytes_sha256, swi_path

AGOGO_SHA256 = "9f0834bc00153de1cae5e9b9b556ff41e5b7ea08fa13bf63ce41a923925975cd"
CROSS_CHECK_NAME = b"CIMBALOM"


def _instrument(**overrides) -> Instrument:
    base = dict(
        name=b"AGOGO   ",
        control=0x1A,
        hr_attack=0,
        hr_decay=0xF,
        hr_sustain=0xF,
        hr_release=0,
        attack=0,
        decay=0,
        sustain=0xF,
        release=0,
        vibrato=0x21,
        vibrato_delay=4,
        arp_speed=2,
        default_chord=1,
        octave_shift=-12,
        gateoff_wf=0,
        gateoff_pw=0,
        gateoff_filt=0,
        first_waveform=0x04,
        wf_table=b"\x41\x00\x00",
        pw_table=b"\x88\x00\x00",
        filter_table=b"\x9f\x03\x01",
    )
    base.update(overrides)
    return Instrument(**base)


def test_build_parse_round_trips_synthetic_instrument():
    ins = _instrument(wf_table=bytes(range(1, 40)))
    payload = build_swi(ins)
    decoded = parse_swi(payload)
    for attr in (
        "name",
        "control",
        "hr_decay",
        "attack",
        "sustain",
        "vibrato",
        "arp_speed",
        "octave_shift",
        "first_waveform",
        "wf_table",
        "pw_table",
        "filter_table",
    ):
        assert getattr(decoded, attr) == getattr(ins, attr), attr
    assert build_swi(decoded) == payload


def test_build_swi_frames_as_load_body_size_name():
    ins = _instrument()
    body = ins.encode()
    out = build_swi(ins)
    assert out[0:2] == SWI_DEFAULT_LOAD_ADDRESS.to_bytes(2, "little")
    assert out[2:-9] == body
    assert out[-9] == len(body)
    assert out[-8:] == ins.name.ljust(8, b" ")


def test_parse_swi_recovers_editor_default_load_address():
    assert SWI_DEFAULT_LOAD_ADDRESS == 0x04A1
    assert parse_swi(build_swi(_instrument())).load_address == 0x04A1


def test_parse_swi_recovers_custom_load_address():
    payload = build_swi(_instrument(), load_address=0x1FF8)
    assert payload[0:2] == (0x1FF8).to_bytes(2, "little")
    assert parse_swi(payload).load_address == 0x1FF8


def test_build_swi_explicit_load_address_overrides_instrument():
    ins = _instrument(load_address=0x1FF8)
    assert build_swi(ins)[0:2] == ins.load_address.to_bytes(2, "little")
    assert build_swi(ins, load_address=0x0B00)[0:2] == (0x0B00).to_bytes(2, "little")


def test_build_swi_uses_instrument_load_address_when_absent():
    ins = _instrument(load_address=0x1234)
    assert build_swi(ins)[0:2] == (0x1234).to_bytes(2, "little")


def test_build_swi_pads_short_name_with_spaces():
    ins = _instrument(name=b"AGOGO")
    assert build_swi(ins)[-8:] == b"AGOGO   "
    assert parse_swi(build_swi(ins)).name == b"AGOGO   "


@pytest.mark.parametrize("addr", [0x10000, 0xFFFFFFFF, -1])
def test_build_swi_rejects_out_of_range_explicit_load_address(addr):
    with pytest.raises(SWMFormatError, match="load_address"):
        build_swi(_instrument(), load_address=addr)


def test_build_swi_rejects_out_of_range_instrument_load_address():
    with pytest.raises(SWMFormatError, match="load_address"):
        build_swi(_instrument(load_address=0x10000))


def test_parse_swi_rejects_non_byte_input():
    with pytest.raises(SWMFormatError, match="byte string"):
        parse_swi("AGOGO")  # type: ignore[arg-type]


def test_parse_swi_rejects_short_input():
    with pytest.raises(SWMFormatError, match="at least"):
        parse_swi(b"\x00" * 20)


def test_parse_swi_rejects_size_byte_mismatch():
    body = b"\x00" * 20
    with pytest.raises(SWMFormatError, match="size byte"):
        parse_swi(b"\xa1\x04" + body + bytes([len(body) - 5]) + b"NAME    ")


def test_build_swi_rejects_oversized_body():
    with pytest.raises(SWMFormatError, match="size byte"):
        build_swi(_instrument(filter_table=b"\x00" * 240))


def test_build_swi_rejects_oversized_name():
    with pytest.raises(SWMFormatError, match="name is"):
        build_swi(_instrument(name=b"TOOLONGNAME"))


def test_read_write_swi_round_trips_via_file(tmp_path: Path):
    ins = _instrument(wf_table=bytes([0x41, 0x02, 0xFF, 0x44]))
    path = tmp_path / "agogo.swi"
    write_swi(ins, path, load_address=0x04A1)
    decoded = read_swi(path)
    assert decoded.name == ins.name
    assert decoded.wf_table == ins.wf_table
    assert decoded.load_address == 0x04A1
    assert path.read_bytes() == build_swi(ins, load_address=0x04A1)


def test_insert_instrument_replaces_existing_slot():
    swm = SWMFile(
        instruments=[
            Instrument(name=b"ONE     "),
            Instrument(name=b"TWO     "),
        ],
    )
    assert insert_instrument(swm, 2, Instrument(name=b"NEW     ")) is swm
    assert swm.instruments[1].name == b"NEW     "
    assert len(swm.instruments) == 2


def test_insert_instrument_appends_next_free_slot():
    swm = SWMFile(instruments=[Instrument(name=b"ONE     ")])
    insert_instrument(swm, 2, Instrument(name=b"TWO     "))
    assert len(swm.instruments) == 2
    assert swm.instruments[1].name == b"TWO     "


def test_insert_instrument_rejects_slot_below_one():
    with pytest.raises(SWMFormatError, match="must be >= 1"):
        insert_instrument(SWMFile(instruments=[Instrument()]), 0, Instrument())


def test_insert_instrument_rejects_nonadjacent_slot():
    swm = SWMFile(instruments=[Instrument()] * 2)
    with pytest.raises(SWMFormatError, match="next free slot"):
        insert_instrument(swm, 5, Instrument())


def test_native_swi_round_trips_byte_exact(swi_bytes: bytes):
    """Every curated editor ``.swi`` round-trips byte-exactly at the editor's load."""
    assert parse_swi(swi_bytes).load_address == 0x04A1
    assert build_swi(parse_swi(swi_bytes)) == swi_bytes


def test_native_swi_size_byte_matches_body_and_name_is_eight(swi_bytes: bytes):
    assert swi_bytes[-9] == len(swi_bytes) - 11
    assert len(swi_bytes[-8:]) == 8
    assert parse_swi(swi_bytes).encode() == swi_bytes[2:-9]


def test_native_agogo_matches_issue_canonical_bytes():
    """Anchor the byte-exact guarantee to the issue's pinned agogo.swi SHA-256."""
    data = swi_path("agogo").read_bytes()
    assert _bytes_sha256(data) == AGOGO_SHA256
    assert parse_swi(data).load_address == 0x04A1
    assert build_swi(parse_swi(data)) == data


def test_cross_check_swi_matches_same_named_swm_instrument(nepzenek_file: Path):
    """Where the same instrument exists in both forms, the records agree."""
    swm = read_swm(nepzenek_file)
    swm_match = [
        ins for ins in swm.instruments if ins.name.rstrip(b" \x00").upper() == CROSS_CHECK_NAME
    ]
    assert swm_match, "nepzenek.swm should contain a CIMBALOM instrument"
    swi_ins = parse_swi(swi_path("cimbalom").read_bytes())
    assert swi_ins.name.rstrip(b" \x00").upper() == CROSS_CHECK_NAME
    assert swm_match[0].encode() == swi_ins.encode()
