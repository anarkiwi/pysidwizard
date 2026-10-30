"""The player-build feature table, transcribed from settings.cfg + altplayers.inc."""

import re
from dataclasses import fields, replace
from pathlib import Path

import pytest

from pysidwizard import parse_sid, parse_sid_song
from pysidwizard.features import (
    BUILD_FLAGS,
    DETECTED_FLAGS,
    DRIVER_BARE,
    DRIVER_DEMO,
    DRIVER_EXTRA,
    DRIVER_LIGHT,
    DRIVER_MEDIUM,
    DRIVER_NORMAL,
    FACETS,
    PlayerFeatures,
    PlayerQuirks,
    detect_features,
    detect_flags,
    detect_quirks,
    features_for_driver,
)
from pysidwizard.sidfile import parse_psid_header
from tests._hvsc_fetch import resolve
from tests._swm_cache import sw197_path

# The seven flags the Extra build (altplayers.inc:542-556) flips versus normal.
EXTRA_DELTA = {
    "portavibra",
    "fastspeedbind",
    "delaysupport",
    "allghostregs",
    "filteralways",
    "pulsealways",
    "vibslidealways",
}


@pytest.mark.parametrize(
    "driver_type,name",
    [
        (DRIVER_NORMAL, "normal"),
        (DRIVER_MEDIUM, "medium"),
        (DRIVER_LIGHT, "light"),
        (DRIVER_EXTRA, "extra"),
        (DRIVER_BARE, "bare"),
        (DRIVER_DEMO, "demo"),
    ],
)
def test_driver_byte_selects_its_build(driver_type, name):
    features = features_for_driver(driver_type)
    assert features.driver_type == driver_type
    assert features.name == name


def test_unknown_driver_byte_falls_back_to_default_build():
    assert features_for_driver(119) is BUILD_FLAGS[DRIVER_NORMAL]
    assert features_for_driver(-1) is BUILD_FLAGS[DRIVER_NORMAL]


def test_extra_build_differs_from_normal_in_exactly_seven_flags():
    normal = BUILD_FLAGS[DRIVER_NORMAL].flag_names()
    extra = BUILD_FLAGS[DRIVER_EXTRA].flag_names()
    differing = {k for k in normal if normal[k] != extra[k]}
    assert differing == {f.upper() + "_ON" for f in EXTRA_DELTA}


def test_bare_build_has_every_optional_feature_off():
    assert not any(BUILD_FLAGS[DRIVER_BARE].flag_names().values())


def test_light_build_drops_chords_vibrato_and_tempo_programs():
    light = BUILD_FLAGS[DRIVER_LIGHT]
    assert not light.chordsupport
    assert not light.transposesupp
    assert not light.calcvibrato
    assert not light.tempoprgsupp
    assert not light.finefiltsweep
    assert light.multispeedsupp


def test_medium_build_keeps_chords_but_drops_hardrestart_types():
    medium = BUILD_FLAGS[DRIVER_MEDIUM]
    assert medium.chordsupport
    assert medium.calcvibrato
    assert not medium.hardrestypes
    assert not medium.frame1switch
    assert not medium.filt_ctrl_fx


def test_demo_build_drops_the_small_fx_groups():
    demo = BUILD_FLAGS[DRIVER_DEMO]
    assert not demo.filter_smallfx
    assert not demo.detune_smallfx
    assert not demo.wfctrl_smallfx
    assert not demo.subtunesupport
    assert demo.multispeedsupp


def test_flag_names_are_the_assembly_names():
    names = BUILD_FLAGS[DRIVER_NORMAL].flag_names()
    assert "FASTSPEEDBIND_ON" in names
    assert "ALLGHOSTREGS_ON" in names
    assert "driver_type" not in names
    assert all(n.endswith("_ON") for n in names)


# Each flag's site as assembled with the flag on and off (1.97 player.asm), operands zeroed.
SITES = {
    "chordsupport": ("3d00009d0000c8b1fec8c97ff012", "3d00009d0000c8b1fec88d0000"),
    "detunesupport": (
        "3d00009d0000c8b1fec8c97ff0128d0000b1fec9fff0039d0000c8989d0000",
        "3d00009d0000c8b1fec88d0000c8989d0000",
    ),
    "arpspeedsupp": ("de00001003bd0000293f9d0000bc0000b1fec9fe", "293f9d0000bc0000b1fec9fe"),
    "hardrestypes": ("a9fe9d00003d00009d0000a002b1fe9d0610", "a9fe9d00003d00009d0000a90f9d05d4"),
    "allghostregs": (
        "a9fe9d00003d00009d0000a002b1fe9d0610",
        "a9fe9d00003d00009d0000a002b1fe9d06d4",
    ),
    "frame1switch": ("29309d0000a91a2908f010", "29309d0000a91aa9099d0000"),
    "wfarp_nop_supp": ("1010c980f005c9e090", "1010c9e090"),
    "octaveshift": ("4c000018a00971fe187d00009d0000", "4c000018187d00009d0000"),
    "transposesupp": ("4c000018a00971fe187d00009d0000", "4c000018a00971fe9d0000"),
    "pwresetsw": ("2c00007008a00ab1fe9d0000", "a00ab1fe9d0000"),
    "filtresetsw": ("a00ab1fe9d00002c00003010", "a00ab1fe9d0000bc0000"),
    "portavibra": ("c0fff010bc0000b90000fd00008d0000", "bc0000b90000fd00008d0000"),
    "pwkeybtrack": ("989d0000a9009d000018bd0000f00a", "989d0000a9009d0000bd0000"),
    "filtkbtrack": ("8d18d418a900f005ae", "8d18d418a90b8d16d4"),
    "finefiltsweep": ("8d16d4a9078d15d4", "8d16d460"),
    "calcvibrato": ("29f04af0044a7d0000", "29f04aa8c0"),
    "fastspeedbind": ("297fc90210034c0000a902bc0000f004c06090", "a902bc0000f004c06090"),
    "delaysupport": ("a9ff9d00008a38e907", "8a38e907"),
    "multispeedsupp": (
        "9d0000a9ff9d00009d0000a007b1fe9d000020",
        "9d0000a9ff9d0000a007b1fe9d000020",
    ),
    "filt_ctrl_fx": ("48290f8d00006829f08d0000", ""),
    "retainzeropage": ("a5fe48a5ff48a20e", "a20e"),
}


@pytest.mark.parametrize("flag", sorted(SITES))
def test_each_flag_reads_from_its_site(flag):
    on, off = (bytes.fromhex(code) for code in SITES[flag])
    assert detect_flags(on, 0x1000)[flag] is True
    assert detect_flags(off, 0x1000).get(flag, False) is False


def test_a_flag_without_its_site_keeps_the_fallback_build():
    """Foreign code (no anchor) leaves every anchored flag to the header's build."""
    found = detect_flags(bytes(64), 0x1000)
    assert found == {"filt_ctrl_fx": False, "retainzeropage": False}
    extra = BUILD_FLAGS[DRIVER_EXTRA]
    assert detect_features(bytes(64), 0x1000, extra) == replace(extra, **found)


# HRENDER ``jmp`` (target patched in), then CNTPLAY ending at offset 32: CNTPLY2 is $1020.
_HRENDER = "bc0000b9000085feb9000085ff4c"
_CNTPLAY = "bc0000d00160b9000085feb9000085ff"
_WFARPTB = "0000000000000000293f9d0000bc"


@pytest.mark.parametrize(
    "target,expected",
    [(0x1020, True), (0x1028, False), (0x1030, None)],
)
def test_vibslidealways_is_hrenders_exit_to_cntply2(target, expected):
    code = bytes.fromhex(_HRENDER + target.to_bytes(2, "little").hex() + _CNTPLAY + _WFARPTB)
    code += bytes(16)
    assert detect_flags(code, 0x1000).get("vibslidealways") is expected


@pytest.mark.parametrize("name", ["bronkosaurus.sid", "glacialblues.sid"])
def test_release_exports_read_as_their_header_build(name):
    """A release's own exports carry the build their driver byte names, every flag read."""
    data = sw197_path(name).read_bytes()
    swm = parse_sid(data)
    assert swm.player.features == features_for_driver(swm.driver_type)
    header = parse_psid_header(data)
    found = detect_flags(data[header.data_start :], header.real_load_address)
    assert set(found) == set(DETECTED_FLAGS)


def test_a_packed_player_below_its_swp_data_reads_its_own_code():
    """Performers Demo 2018's SWP data follows its player, so the module window starts there."""
    path = resolve("MUSICIANS/C/C0zmo/Performers_Demo_2018.sid")
    if path is None:
        pytest.skip("unreachable (offline)")
    swm = parse_sid_song(path.read_bytes(), 1)
    assert not swm.player.features.detunesupport and not swm.player.features.multispeedsupp
    assert swm.player.freq_lo[0x7D] == 0x22  # FREQTBL+$7D, past the 96 notes


def test_a_packed_game_player_reads_as_its_own_build():
    """Arkanoid Remake's SWP player is an edited build, though its header names Extra."""
    path = resolve("MUSICIANS/C/C0zmo/Arkanoid_Remake.sid")
    if path is None:
        pytest.skip("unreachable (offline)")
    swm = parse_sid_song(path.read_bytes(), 1)
    assert swm.driver_type == DRIVER_EXTRA
    off = ("hardrestypes", "vibslidealways", "arpspeedsupp", "detunesupport", "allghostregs")
    assert not any(getattr(swm.player.features, flag) for flag in off)
    assert swm.player.features.chordsupport and swm.player.features.frame1switch


_SETFLTP = "c9fff00e8e97148c9b14bd87100d0711d006"
_STOPFLT = "ec9714"
_FSWITCH = "b1fe1003c990b004290f8d0000"
_KBTRACK = "c8b1fe8d0000c88c0000"


@pytest.mark.parametrize(
    "code, changed",
    [
        ("", {}),
        (_SETFLTP + "bd8810" + _SETFLTP + _STOPFLT, {"filter_stop_rewinds": False}),
        (_SETFLTP + _STOPFLT + _SETFLTP + "bd8810", {}),
        (_FSWITCH, {}),
        (_KBTRACK, {"kbtrack_fswitch": False}),
        (_KBTRACK + _FSWITCH, {}),
        ("2908f010a9099d", {"first_frame_fixed_waveform": True}),
        ("bd4b10f005c92e100c9d4c10", {"sel_ins_limit": 0x2E}),
    ],
)
def test_each_quirk_reads_from_its_site(code, changed):
    """A quirk without its site keeps its default; the first SETFLTP decides STOPFLT."""
    assert detect_quirks(bytes.fromhex(code), b"") == replace(PlayerQuirks(), **changed)


# Facets recorded per build (settings.cfg / altplayers.inc) that the Python player has no behaviour for.
UNMODELLED = {
    "portame_notefx",
    "tempoprgsupp",
    "subtunejump",
    "subtunesupport",
    "filteralways",
    "pulsealways",
    "filtshift_supp",
    "seq_fx_support",
    "volset_support",
    "vibfreqfx_supp",
    "filter_smallfx",
    "detune_smallfx",
}
_PLAYER_SRC = Path(__file__).resolve().parent.parent / "src" / "pysidwizard"


def _player_references(kind: str) -> set:
    """The attributes the player reads off ``features`` / ``quirks``."""
    pattern = re.compile(rf"\bself\.(?:image\.)?{kind}\.(\w+)")
    refs: set = set()
    for path in (_PLAYER_SRC / "player").glob("*.py"):
        refs.update(pattern.findall(path.read_text()))
    return refs


def test_registry_and_player_references_match_both_ways():
    """Every facet is a field the player reads (or is recorded only) and every read is a facet."""
    builds = {f.name for f in FACETS.of_kind("build")}
    releases = {f.name for f in FACETS.of_kind("release")}
    assert {f.name for f in fields(PlayerFeatures)} - {"driver_type", "name"} == builds
    assert {f.name for f in fields(PlayerQuirks)} == releases
    assert _player_references("features") == builds - UNMODELLED
    assert _player_references("quirks") == releases
