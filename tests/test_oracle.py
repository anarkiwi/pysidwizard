"""Byte-exact comparison of the native SWMPlayer against the sidtrace oracle.

Marked ``oracle``: these tests need Docker (the ``anarkiwi/sidtrace`` image) and
HVSC ``.sid`` tunes, so the default suite excludes them (see ``pyproject``); a
dedicated CI job runs ``pytest -m oracle``. They are never skipped -- an
unavailable tune or a failed oracle render fails the test rather than hiding a
regression. HVSC ``.sid`` files are copyright works: they are downloaded to a
cache (or a local ``$HVSC`` tree), never committed.

The sidtrace oracle renders a PSID/RSID container through ``sidplayfp``; the four
bundled SID-Wizard example tunes are raw ``.swm`` modules it cannot read, so the
native player is validated against real HVSC SID-Wizard ``.sid`` exports instead.
:data:`TUNES` is the curated set of such exports whose native
:class:`~pysidwizard.player.SWMPlayer` render matches the oracle frame-for-frame
(built by running this comparison over the ``tests/_hvsc_corpus`` sample and
keeping the byte-exact matches; the rest differ by driver version or the
reader's lossy recovery and are excluded).

The oracle grid leads the native render by the SID-Wizard driver's init/setup
frames: the native player starts at musical frame 0 (as the old asid-vice
harness did with ``skip_transition``), so the match trims up to :data:`MAX_LEAD`
leading oracle frames. It also excuses the single first musical frame, where the
oracle still carries the driver's init-register residue in registers the tune
has not written yet (the old harness ghost-seeded that residue); every remaining
frame must match byte-for-byte.
"""

import os
from pathlib import Path

import pytest
from pysidtracker.testing import TuneFetchError, oracle_grid, resolve_tune

from pysidwizard import SWMPlayer, parse_sid

# Cache under the workspace (a Docker-daemon-visible path, and what CI persists
# via actions/cache). ``$PYSIDWIZARD_ORACLE_CACHE`` overrides the location.
_CACHE = Path(os.environ.get("PYSIDWIZARD_ORACLE_CACHE", ".oracle-cache"))

# Frames compared per tune, and the max leading oracle init/setup frames trimmed.
FRAMES = 250
MAX_LEAD = 4

# HVSC SID-Wizard exports the native player reproduces byte-exactly against the
# deterministic sidtrace oracle (curated from tests/_hvsc_corpus.SAMPLE).
TUNES = {
    "8_bit_bard": "DEMOS/0-9/8-Bit_Bard.sid",
    # Exercises BIGFX05 (WRITEAD) alongside BIGFX02/03; matched only once the
    # $04/$05/$06 register-write effects were modelled.
    "cloud_9": "MUSICIANS/A/Ash_9/Cloud_9.sid",
    "oua_oua_song": "DEMOS/M-R/Oua_Oua_Song.sid",
    "pentagorat_ii": "GAMES/M-R/Pentagorat_II.sid",
    "burn": "MUSICIANS/D/DAM/Burn.sid",
    "must_dash": "MUSICIANS/D/DAM/Must_Dash.sid",
    "taming_the_fire": "MUSICIANS/D/Dave_Sidnify/Taming_the_Fire.sid",
    "ready_to_go": "MUSICIANS/G/Goerp/Ready_to_Go.sid",
    "csillag": "MUSICIANS/H/Hermit/Csillag_szazketto_kettoskereszt.sid",
    "rodman_jr_plus": "MUSICIANS/M/Misfit/Rodman_Jr_plus.sid",
    "death_in_rome": "MUSICIANS/S/Stone_James/Death_in_Rome.sid",
    "star_storm": "GAMES/S-Z/Star_Storm.sid",
    # Exercises BIGFX11 (main funktempo); matched only once the tempo FX landed.
    "moofistication": "MUSICIANS/S/Skuggemannen/Moofistication.sid",
    "cyberiad_theory": "MUSICIANS/L/LukHash/Cyberiad_Theory.sid",
    "perpetual_motion": "MUSICIANS/L/LukHash/Perpetual_Motion.sid",
    "switchback_remake": "MUSICIANS/V/Vincenzo/Switchback_Remake.sid",
}


def _match_lead(oracle, rendered, max_lead=MAX_LEAD):
    """Smallest lead with ``oracle[lead+1:lead+N] == rendered[1:N]``, else ``None``.

    Trims the driver's leading init/setup frames the native player skips and
    excuses only the first musical frame (its un-written init-register residue);
    every other frame must match byte-for-byte.
    """
    n = len(rendered)
    for lead in range(max_lead + 1):
        segment = oracle[lead : lead + n]
        if len(segment) == n and segment[1:] == rendered[1:]:
            return lead
    return None


@pytest.fixture(params=list(TUNES))
def tune_id(request):
    return request.param


@pytest.mark.oracle
def test_native_render_matches_oracle(tune_id):
    path = resolve_tune(TUNES[tune_id], cache_dir=_CACHE / "hvsc", local_env="HVSC")
    if path is None:
        raise TuneFetchError(f"tune {tune_id} unavailable (offline, not cached)")
    seconds = (FRAMES + MAX_LEAD) // 50 + 2
    expected = oracle_grid(
        path, oracle_cache=_CACHE / "csv", seconds=seconds, frames=FRAMES + MAX_LEAD
    )
    rendered = SWMPlayer(parse_sid(Path(path).read_bytes())).render_grid(FRAMES)
    assert (
        _match_lead(expected, rendered) is not None
    ), f"tune {tune_id}: native SWMPlayer render does not match the sidtrace oracle"
