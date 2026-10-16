"""Tests for the ``scripts/hvsc_triage.py`` not-decompiled classifier (issue #52).

The pure functions are tested directly (no pool, no network); the real-tune
classification tests resolve a local ``$HVSC`` tree or the HVSC mirror and skip
cleanly when a tune is genuinely unreachable, mirroring :mod:`tests.test_hvsc_corpus`.
"""

# pylint: disable=missing-function-docstring

import sys
from pathlib import Path

import pytest

from tests._hvsc_fetch import resolve

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import hvsc_triage as ht  # noqa: E402  # pylint: disable=wrong-import-order,wrong-import-position


def test_songs_enumerates_1_based():
    assert ht.songs({"songs": "3"}) == [1, 2, 3]
    assert ht.songs({"songs": "1"}) == [1]


def test_is_player_matches_case_insensitively():
    assert ht._is_player("Hermit/SidWizard_V1.x", "SidWizard")
    assert ht._is_player("hermit/sidwizard", "SIDWIZARD")
    assert not ht._is_player("Other/Player", "SidWizard")


def test_load_tunes_reads_find_csv(tmp_path):
    csv = tmp_path / "tunes.csv"
    csv.write_text(
        "path,md5,player,songs,clock,lengths\n"
        "DEMOS/0-9/8-Bit_Bard.sid,abcd,Hermit/SidWizard_V1.x,1,PAL,60\n",
        "utf-8",
    )
    rows = ht.load_tunes(csv)
    assert len(rows) == 1
    assert rows[0]["path"] == "DEMOS/0-9/8-Bit_Bard.sid"
    assert rows[0]["songs"] == "1"


def test_write_csv_roundtrips(tmp_path):
    rows = [ht.Result("A.sid", 1, "not_decompiled", "err"), ht.Result("B.sid", 2, "ok", "")]
    out = tmp_path / "out.csv"
    ht.write_csv(rows, out)
    text = out.read_text("utf-8").strip().splitlines()
    assert text[0] == "path,song,verdict,error"
    assert text[1].startswith("A.sid,1,not_decompiled,")


def test_classify_empty_bytes_is_not_decompiled():
    # parse_sid_song on empty bytes raises SIDFormatError (a SidParseError).
    verdict, error = ht.classify(b"", 1)
    assert verdict == ht.NOT_DECOMPILED
    assert error.startswith("SIDFormatError")


def test_classify_library_error_for_non_sid_parse_error():
    # A non-SidParseError (e.g. a bug) is library_error, not not_decompiled.
    import pysidwizard

    def boom(_data, _song):
        raise RuntimeError("simulated bug")

    monkeypatched = ht
    monkeypatched.parse_sid_song = boom  # type: ignore[assignment]
    try:
        verdict, error = monkeypatched.classify(b"whatever", 1)
    finally:
        monkeypatched.parse_sid_song = pysidwizard.parse_sid_song
    assert verdict == ht.LIBRARY_ERROR
    assert error.startswith("RuntimeError")


def test_tune_rows_no_match_player_is_empty(tmp_path):
    assert (
        ht.tune_rows(
            tmp_path, "zzz_no_such_player", {"path": "X.sid", "player": "a/b", "songs": "1"}
        )
        == []
    )


def test_tune_rows_missing_file_is_empty(tmp_path):
    row = {"path": "NO/SUCH.sid", "player": "Hermit/SidWizard_V1.x", "songs": "1"}
    assert ht.tune_rows(tmp_path, ht.PLAYER, row) == []


def _path(rel):
    p = resolve(rel)
    if p is None:
        pytest.skip(f"{rel} unreachable (local HVSC or mirror)")
    return p


def test_classify_ok_tune_is_ok():
    data = _path("DEMOS/0-9/8-Bit_Bard.sid").read_bytes()
    assert ht.classify(data, 1) == (ht.OK, "")


def test_classify_multi_sid_is_not_decompiled():
    data = _path("MUSICIANS/T/Televicious/Eviltwin_2SID.sid").read_bytes()
    verdict, error = ht.classify(data, 1)
    assert verdict == ht.NOT_DECOMPILED
    assert "multi-SID" in error


def test_not_decompiled_reports_only_the_multi_sid_of_a_mixed_csv():
    ok = "DEMOS/0-9/8-Bit_Bard.sid"
    multi = "MUSICIANS/T/Televicious/Eviltwin_2SID.sid"
    p_ok, p_multi = resolve(ok), resolve(multi)
    if p_ok is None or p_multi is None:
        pytest.skip("ok or multi-SID tune unreachable")
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        hvsc = Path(d)
        (hvsc / "DEMOS/0-9").mkdir(parents=True)
        (hvsc / "MUSICIANS/T/Televicious").mkdir(parents=True)
        (hvsc / ok).write_bytes(p_ok.read_bytes())
        (hvsc / multi).write_bytes(p_multi.read_bytes())
        csv = hvsc / "tunes.csv"
        csv.write_text(
            "path,md5,player,songs,clock,lengths\n"
            f"{ok},a,Hermit/SidWizard_V1.x,1,PAL,60\n"
            f"{multi},b,Hermit/SidWizard_V1.x,1,PAL,60\n",
            "utf-8",
        )
        rows = ht.not_decompiled(hvsc, csv, jobs=1)
    assert rows and all(r.verdict == ht.NOT_DECOMPILED for r in rows)
    assert [r.path for r in rows] == [multi]
