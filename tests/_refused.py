"""The HVSC SID-Wizard songs the binder refuses (``tests/_refused_songs.py``) and the check
that each raises its issue's refusal."""

import pytest

from pysidwizard import SIDFormatError, SWMPlayer
from tests._refused_songs import REFUSED

# A refused song raises within this many play calls (a wrapper may reach PLAYER late).
CALLS = 60
# The binder's refusal text for each issue's builds.
REASONS = {
    121: "partly relocated player is not stated",
    122: "no SID-Wizard big effect",
    123: "no SID-Wizard",
    124: "no SID-Wizard",
    125: "multi-SID",
}


def _play(swm) -> None:
    player = SWMPlayer(swm)
    for _ in range(CALLS):
        player.play_frame()


def assert_refused(path: str, swm) -> bool:
    """For a refused HVSC ``path``: each refused song raises its issue's refusal and every
    other subtune plays; True. False for a tune every song of which binds."""
    songs = REFUSED.get(path)
    if songs is None:
        return False
    for n in range(swm.subtune_count):
        if n + 1 in songs:
            with pytest.raises((ValueError, SIDFormatError), match=REASONS[songs[n + 1]]):
                _play(swm.subtune(n))
        else:
            _play(swm.subtune(n))
    return True


# Tunes whose interrupt reaches no modelled entry: SpecPlayer plays their own 6502 without
# the spec (pysidtracker#187; the build is #124's).
SPEC_NEVER_RUNS = {"MUSICIANS/D/Deetsay/Asmlook.sid"}


def assert_spec_ran(path: str, player) -> None:
    """The spec played ``player``'s calls (a modelled entry ran), except for the tunes of
    :data:`SPEC_NEVER_RUNS`, whose calls it does not reach."""
    assert (player.entry is None) == (path in SPEC_NEVER_RUNS)


__all__ = ["REASONS", "REFUSED", "SPEC_NEVER_RUNS", "assert_refused", "assert_spec_ran"]
