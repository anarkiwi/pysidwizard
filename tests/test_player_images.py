"""Issue #56: a bare ``.swm`` pulls the release's per-driver FREQTBL image.

A bare ``.swm`` carries no player image, so past-``FREQTBL`` lookups would read
a constant fallback; ``parse_swm`` now applies the released image so a native
module renders as the editor's ``.sid`` export plays it.
"""

from __future__ import annotations

import pytest

from pysidwizard import PlayerImage, SWMFile, SWMPlayer, parse_swm, parse_swp
from pysidwizard.features import features_for_driver
from pysidwizard.player import NOTE_FREQ_LO
from pysidwizard.player_images import LATEST_PLAYER_VERSION, image_for
from pysidwizard.reader import apply_player_image
from pysidwizard.sidreader import parse_sid
from tests._swm_cache import sw197_path, swp_path

_FRAMES = 900


def _frames(swm: SWMFile, n: int = _FRAMES):
    return [list(f) for f in SWMPlayer(swm).iter_frames(n)]


def test_image_for_197_driver0_matches_sid():
    """The 1.97/driver-0 image (issue's unit test) matches the .sid export."""
    sid = parse_sid(sw197_path("bronkosaurus.sid").read_bytes())
    lo, hi, first_frame = image_for("1.97", 0)
    assert lo[:96] == NOTE_FREQ_LO
    assert lo[96:] == sid.player.freq_table_tail
    assert first_frame == sid.player.quirks.first_frame_freq_by_track
    assert image_for(LATEST_PLAYER_VERSION, sid.driver_type) == image_for("1.97", 0)
    assert hi and lo  # both spans are the full 256-byte FREQTBL/FREQTBH


def test_image_for_missing_pairs_is_none():
    assert image_for("1.97", 1) is None  # 1.97 medium has no 1-SID export
    assert image_for("2.99", 0) is None  # unknown release
    assert image_for("1.97", 42) is None


@pytest.mark.parametrize("name", ["bronkosaurus", "glacialblues"])
def test_native_swm_renders_like_sid(name):
    swm = parse_swm(sw197_path(name).read_bytes())
    sid = parse_sid(sw197_path(name + ".sid").read_bytes())
    assert swm.player.freq_lo and swm.player.freq_hi
    assert _frames(swm) == _frames(sid)


def test_bare_swm_differs_without_release_image():
    data = sw197_path("bronkosaurus").read_bytes()
    released = parse_swm(data)
    fallback = parse_swm(data, player_version=None)
    assert released.player.freq_lo and fallback.player is None
    assert _frames(fallback) != _frames(released)


def test_apply_player_image_sets_released_image():
    swm = SWMFile(driver_type=0)
    apply_player_image(swm, "1.97")
    image = swm.player
    assert (image.freq_lo, image.freq_hi, image.quirks.first_frame_freq_by_track) == image_for(
        "1.97", 0
    )
    assert image.features == features_for_driver(0)


def test_apply_player_image_respects_an_existing_image():
    swm = SWMFile(
        driver_type=0,
        player=PlayerImage(features_for_driver(0), freq_lo=b"\x00" * 256, freq_hi=b"\x00" * 256),
    )
    apply_player_image(swm, "1.97")
    assert swm.player.freq_lo == b"\x00" * 256


def test_apply_player_image_unshipped_pair_is_noop():
    swm = SWMFile(driver_type=1)
    apply_player_image(swm, "1.97")
    assert swm.player is None


def test_apply_player_image_none_version_is_noop():
    swm = SWMFile(driver_type=0)
    apply_player_image(swm, None)
    assert swm.player is None


def test_parse_swp_applies_the_released_image():
    swm = parse_swp(swp_path("flashitback.swp").read_bytes())
    assert swm.player.freq_lo and swm.player.freq_hi
    assert (swm.player.freq_lo, swm.player.freq_hi) == image_for(
        LATEST_PLAYER_VERSION, swm.driver_type
    )[:2]
