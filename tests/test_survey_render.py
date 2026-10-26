"""The ``scripts/oracle_hvsc.py`` survey renderer and its render contract."""

# pylint: disable=missing-function-docstring

import json
import re
import sys
from pathlib import Path

import numpy as np
import pytest
from pysidtracker import SidParseError, cadence
from pysidtracker.cadence import Cadence
from pysidtracker.registers import PAL_CLOCK_HZ, PAL_CYCLES_PER_FRAME
from pysidtracker.sidid import sidid_signatures

from pysidwizard import SIDFormatError, SWMPlayer, parse_sid
from tests._hvsc_corpus import MULTI_SID
from tests._hvsc_fetch import resolve

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import oracle_hvsc  # noqa: E402  # pylint: disable=wrong-import-order,wrong-import-position


def _tune(relpath):
    path = resolve(relpath)
    if path is None:
        pytest.skip(f"{relpath} unreachable (offline)")
    return path.read_bytes()


def _native(data, subtune, nframes):
    return SWMPlayer(parse_sid(data).subtune(subtune)).render_grid(nframes)


@pytest.mark.parametrize(
    "relpath, info",
    [
        (
            "DEMOS/0-9/8-Bit_Bard.sid",
            {"driver": 0, "frame_speed": 1, "rsid": False, "trigger": "pal_video", "speed": 1},
        ),
        (
            "MUSICIANS/G/Gregfeel/Bad_Sector.sid",
            {"driver": 0, "frame_speed": 1, "rsid": True},
        ),
        (
            "MUSICIANS/F/Flipside/Skybreaker_21.sid",
            {"driver": 3, "frame_speed": 4, "rsid": False, "speed": 4},
        ),
    ],
)
def test_render_matches_player_and_reports_build(relpath, info):
    data = _tune(relpath)
    grid, got = oracle_hvsc.render(data, 1, 60)
    assert grid.dtype == np.int16 and grid.shape == (60, 25)
    np.testing.assert_array_equal(grid, _native(data, 0, 60))
    assert json.loads(json.dumps(got)) == got
    assert {k: got[k] for k in info} == info
    assert isinstance(got["dynamic"], bool)


def test_render_reports_unknown_trigger(monkeypatch):
    data = _tune("DEMOS/0-9/8-Bit_Bard.sid")
    unknown = Cadence(PAL_CYCLES_PER_FRAME, None, PAL_CLOCK_HZ)
    monkeypatch.setattr(cadence, "playroutine_cadence", lambda *_, **__: unknown)
    grid, info = oracle_hvsc.render(data, 1, 20)
    np.testing.assert_array_equal(grid, _native(data, 0, 20))
    assert (info["trigger"], info["speed"]) == (None, 1)


def test_render_selects_one_based_subtune():
    data = _tune("GAMES/M-R/Pentagorat_II.sid")
    second = oracle_hvsc.render(data, 2, 100)[0]
    np.testing.assert_array_equal(second, _native(data, 1, 100))
    assert (second != oracle_hvsc.render(data, 1, 100)[0]).any()


@pytest.mark.parametrize("data", [b"\x00\x18junk", b"PSID", b"PSID" + bytes(0x80)])
def test_non_sidwizard_raises_sid_parse_error(data):
    with pytest.raises(SidParseError):
        oracle_hvsc.render(data, 1, 10)


def test_multi_sid_is_not_decompiled():
    with pytest.raises(SidParseError, match="SID playback is not modelled"):
        oracle_hvsc.render(_tune(MULTI_SID[0]), 1, 10)


def test_parse_error_is_survey_not_decompiled():
    assert issubclass(SIDFormatError, SidParseError)


def test_player_regex_selects_sidwizard_signatures():
    names = [name for name, _ in sidid_signatures()]
    matched = [n for n in names if re.search(oracle_hvsc.PLAYER, n, re.I)]
    assert "Hermit/SidWizard_V1.x" in matched
    assert all("SidWizard" in n for n in matched)
    assert not [n for n in names if "SidWizard" in n and n not in matched]
