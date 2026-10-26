"""Issue #59: SID-Maker's PRG exports read into the same model as its ``.sid`` export.

``.exe.prg`` (and SID-Maker-Echo's RSID ``.sid``) copy a ``SIDHEADER`` image into place from a
BASIC starter; the plain PRG export is the player+tune at ``PLAYERADDR``. Strict xfails mark
what #59 still has to land.
"""

from __future__ import annotations

import numpy as np
import pytest
from pysidtracker.survey import as_sid, file_oracle

from pysidwizard import SIDFormatError, SWMPlayer, is_sidwizard_sid, parse_sid, parse_swm
from tests._swm_cache import sw197_export_path

SINGLE_SID = (
    "bronkosaurus",
    "c0z-50shades",
    "cloudlessr",
    "congabeat",
    "demo-example",
    "euphoria",
    "flashitback",
    "futuco",
    "glacialblues",
    "kerekesintro",
    "kicksamule",
    "lenore",
    "nepzenek",
    "oakyardmemo",
    "pimpmycomm",
    "rain8580",
    "versenyvagym",
    "vn-waiting",
)
MULTI_SID = (
    "childhood-2SID",
    "egblues-2SID",
    "newwave-3SID",
    "ramos-3SID",
    "techno-3SID",
    "thizizda-3SID",
)
EXE = "native/examples/prg-exports/{}.exe.prg"
SID = "native/examples/sid-exports/{}.sid"
ECHO_EXE, ECHO_SID = EXE.format("echo-test"), SID.format("echo-test")
ECHO_SWM = "native/examples/echo-test.swm"
BARE_PRG = "native/sources/SFX-example/pimpnotersfx.prg"
FRAMES = 1500


def _data(member: str) -> bytes:
    return sw197_export_path(member).read_bytes()


def _grid(swm, frames: int = FRAMES):
    return np.asarray(SWMPlayer(swm).render_grid(frames), np.int16)


@pytest.mark.parametrize("name", SINGLE_SID)
def test_exe_prg_reads_as_its_sid_export(name):
    assert parse_sid(_data(EXE.format(name))) == parse_sid(_data(SID.format(name)))


@pytest.mark.parametrize("name", MULTI_SID)
def test_multi_sid_exe_prg_is_rejected_as_multi_sid(name):
    with pytest.raises(SIDFormatError, match="multi-SID"):
        parse_sid(_data(EXE.format(name)))


@pytest.mark.parametrize(
    "member", [EXE.format(n) for n in SINGLE_SID + ("rastarok", "echo-test")] + [BARE_PRG]
)
def test_prg_export_is_recognised(member):
    assert is_sidwizard_sid(_data(member))


def test_echo_exe_prg_reads_as_its_sid_export():
    assert parse_sid(_data(ECHO_EXE)) == parse_sid(_data(ECHO_SID))


def test_echo_sid_reads_as_native_swm():
    sid = parse_sid(_data(ECHO_SID))
    swm = parse_swm(_data(ECHO_SWM))
    assert sid.load_address == 0x1400
    assert (sid.sequences, sid.patterns) == (swm.sequences, swm.patterns)
    assert np.array_equal(_grid(sid), _grid(swm))


def test_wrapped_bare_prg_renders_as_its_own_player():
    sid = as_sid(_data(BARE_PRG))
    assert np.array_equal(_grid(parse_sid(sid)), file_oracle(sid, 1, FRAMES)[1])


def test_bare_prg_renders_as_its_own_player():
    prg = _data(BARE_PRG)
    assert np.array_equal(_grid(parse_sid(prg)), file_oracle(as_sid(prg), 1, FRAMES)[1])
