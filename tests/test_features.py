"""The player-build feature table, transcribed from settings.cfg + altplayers.inc."""

import pytest

from pysidwizard.features import (
    BUILD_FLAGS,
    DRIVER_BARE,
    DRIVER_DEMO,
    DRIVER_EXTRA,
    DRIVER_LIGHT,
    DRIVER_MEDIUM,
    DRIVER_NORMAL,
    features_for_driver,
)

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
