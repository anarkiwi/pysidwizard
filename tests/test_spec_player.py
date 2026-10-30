"""SWMPlayer is the playspec on the tune's own image: its 6502, the format hook, standard notes."""

import pytest
from pysidtracker import SidImage, parse_sid_header
from pysidtracker.notes import frequency
from pysidtracker.registers import PAL_CLOCK_HZ

from pysidwizard import (
    Instrument,
    Pattern,
    Row,
    SWMFile,
    SWMFormatError,
    SWMPlayer,
    build_swm,
    parse_swm,
    sidmaker,
)
from pysidwizard.formats import FORMAT
from tests._swm_cache import TUNE_NAMES, swm_path
from tests._toy import assert_6502, note_word, toy_swm

FRAMES = 60
DRIVERS = range(6)
BUILDS = [
    (
        pytest.param(
            version,
            driver,
            marks=pytest.mark.xfail(
                strict=True, raises=ValueError, reason="binder: build not bound"
            ),
        )
        if (version, driver) == ("1.7", 1) or (version != "1.7" and driver == 2)
        else (version, driver)
    )
    for version in sidmaker.VERSIONS
    for driver in DRIVERS
    if (version, driver) != ("1.7", 5)
]


def _scale() -> SWMFile:
    """Every fourth note of the table, one per row, on a plain pulse instrument."""
    swm = toy_swm()
    swm.instruments = [Instrument(sustain=0xF, first_waveform=0x41, wf_table=b"\x41\x00\xff")]
    rows = [Row(note=n, instrument=1) for n in range(1, 96, 4)]
    swm.patterns = [Pattern(rows=rows, length=len(rows))]
    return swm


@pytest.mark.parametrize(("version", "driver"), BUILDS)
def test_every_sid_maker_build_plays_as_its_6502(version, driver):
    swm = toy_swm(driver_type=driver)
    swm.player_version = version
    assert_6502(swm, FRAMES)


@pytest.mark.parametrize("tune", TUNE_NAMES)
def test_example_modules_play_as_their_6502(tune):
    assert_6502(parse_swm(swm_path(tune).read_bytes()), 4 * FRAMES)


def test_unknown_sid_maker_release_is_refused():
    with pytest.raises(ValueError, match="no SID-Maker"):
        parse_swm(build_swm(toy_swm()), "1.0")


def test_multi_sid_playback_is_refused():
    swm = toy_swm()
    swm.sid_amount = 2
    with pytest.raises(SWMFormatError, match="2-SID playback"):
        SWMPlayer(swm)


def test_spec_hook_binds_the_tunes_own_image():
    swm = toy_swm()
    player = SWMPlayer(swm)
    binding = FORMAT.spec(swm)
    image = SidImage.from_bytes(player.sid)
    header = parse_sid_header(player.sid)
    assert binding.image == image.image
    assert (binding.subtune, binding.psid) == (0, True)
    assert FORMAT.model_metadata(swm) == (
        header.name,
        header.author,
        header.released,
        image.load,
        header.init_address,
        header.play_address,
    )


def test_native_spec_player_is_unchanged():
    swm = _scale()
    assert FORMAT.spec_player(swm).render_grid(FRAMES) == SWMPlayer(swm).render_grid(FRAMES)


@pytest.mark.xfail(strict=True, reason="notes() not declared yet")
def test_standard_mode_reads_the_equal_tempered_word():
    """Each FREQTBL index ``i`` the native player reads answers note ``i - 1`` in standard mode."""
    swm = _scale()
    native = SWMPlayer(swm)
    table = [note_word(SWMPlayer(swm), i) for i in range(96)]
    standard = FORMAT.spec_player(swm, notes=PAL_CLOCK_HZ)
    pairs = {
        (row[0] | row[1] << 8, std[0] | std[1] << 8)
        for row, std in zip(native.render_grid(72), standard.render_grid(72), strict=True)
    }
    read = {(table.index(word), got) for word, got in pairs if word in table}
    assert len(read) > 20
    assert all(got == frequency(i - 1, PAL_CLOCK_HZ) for i, got in read)
