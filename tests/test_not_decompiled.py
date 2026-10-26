"""Issue #52: the HVSC survey's ``not_decompiled`` songs, one strict xfail per root cause.

Each single-SID case is checked the way ``pysidtracker.survey`` checks it: the
``scripts/oracle_hvsc.py`` render against the tune's own 6502 player (jennings).
"""

# pylint: disable=missing-function-docstring

import sys
from pathlib import Path

import pytest
from pysidtracker.survey import compare_file

from pysidwizard import SIDFormatError, parse_sid, parse_sid_song, parse_swm
from tests._hvsc_fetch import resolve
from tests._swm_cache import multi_sid_path, sw197_export_path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import oracle_hvsc  # noqa: E402  # pylint: disable=wrong-import-order,wrong-import-position

FRAMES = 1000
METHODIST = "MUSICIANS/M/Mac_TUGCS/Methodist.sid"
SEUCK_CAMP = "MUSICIANS/M/Mac_TUGCS/SEUCK_Camp.sid"
TRANSFORMING = "MUSICIANS/C/C0zmo/Transforming.sid"
BRICKS = "MUSICIANS/N/NecroPolo/Bricks_n_Balls.sid"

PARTIAL_RELOCATION = [(METHODIST, s) for s in (*range(2, 9), 21)] + [
    ("MUSICIANS/V/Vincenzo/Farming_Simulator_6581.sid", 2),
    ("MUSICIANS/V/Vincenzo/Farming_Simulator_8580.sid", 2),
]
WRAPPER_JUMP_TABLE = [(SEUCK_CAMP, s) for s in range(2, 6)]
MULTI_SID = [
    ("childhood-2SID", "childhood.sws"),
    ("egblues-2SID", "egblues.sws"),
    ("newwave-3SID", "newwave.swt"),
    ("ramos-3SID", "ramos.swt"),
    ("techno-3SID", "techno.swt"),
    ("thizizda-3SID", "thizizda.swt"),
]


def _path(rel: str) -> Path:
    path = resolve(rel)
    if path is None:
        pytest.skip(f"{rel} unreachable (offline)")
    return path


def _compare(rel: str, song: int) -> dict:
    return compare_file(_path(rel), oracle_hvsc.render, frames=FRAMES, song=song)


def _xfail(reason: str, raises=AssertionError):
    return pytest.mark.xfail(strict=True, raises=raises, reason=f"#52: {reason}")


@pytest.mark.parametrize("rel, song", PARTIAL_RELOCATION)
def test_partially_relocated_module_song_is_exact(rel, song):
    assert _compare(rel, song)["verdict"] == "exact"


@pytest.mark.parametrize("rel, song", WRAPPER_JUMP_TABLE)
def test_wrapper_dispatched_module_song_is_exact(rel, song):
    assert _compare(rel, song)["verdict"] == "exact"


def test_markerless_player_song_tracks_its_oracle_as_far_as_song_1():
    first = _compare(TRANSFORMING, 1)
    second = _compare(TRANSFORMING, 2)
    assert first["verdict"] == "differs"
    assert second["verdict"] in ("exact", "differs")
    assert second["first_diff"] == first["first_diff"]


def test_foreign_player_song_is_rejected_as_such():
    with pytest.raises(SIDFormatError, match="no SID-Wizard player"):
        parse_sid_song(_path(BRICKS).read_bytes(), 15)


@_xfail("multi-SID .sid exports are rejected", raises=SIDFormatError)
@pytest.mark.parametrize("sid, native", MULTI_SID)
def test_multi_sid_export_reads_its_native_tables(sid, native):
    got = parse_sid(sw197_export_path(f"native/examples/sid-exports/{sid}.sid").read_bytes())
    want = parse_swm(multi_sid_path(native).read_bytes())
    assert got.sid_amount == want.sid_amount
    assert (got.sequences, got.chord_table, got.tempo_table) == (
        want.sequences,
        want.chord_table,
        want.tempo_table,
    )
