"""End-to-end integration test: real SID-Wizard on asid-vice -> pysidwizard.

For each of the four reference tunes:

1. Fetch the SWM from the SID-Wizard 1.94 source tarball
   (delegated to ``sidwizard-driver``).
2. Drive real SID-Wizard inside a ``anarkiwi/headlessvice`` Docker
   container via ``sidwizard-driver`` to capture a fresh
   ``frame,reg,value`` reference CSV.
3. Assert pysidwizard's ``SWMPlayer`` produces the same SID-register
   state as the freshly captured reference, every frame, every
   register — proves pysidwizard reproduces what the real player
   would write.

Each tune is verified for its *entire* playthrough length: songs that
hit an ``End`` (flashitback, euphoria) are captured to their natural
end; songs that loop forever (bronkosaurus, rain8580) are capped at
``MAX_SONG_FRAMES``. See :func:`_song_length_frames`.

This is the "every PR" enforceable-100%-reproduction gate. It's the
slow test (several minutes' wall time per tune, scaling with song
length plus the tarball download): opt in with
``pytest -m integration tests/integration/``. CI runs it on every push
+ PR via ``.github/workflows/integration.yml``.

Requirements:

- Docker daemon reachable.
- ``anarkiwi/headlessvice:latest`` image pulled (or fetchable).
- A free TCP port for ``binmon`` (defaults to 6612, configurable
  via ``SIDWIZARD_BINMON_PORT``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pysidwizard.player import SWMPlayer
from pysidwizard.reader import read_swm
from tests._diff_harness import diff_against_reference, load_reference_csv
from tests._swm_cache import TUNE_NAMES, swm_path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"

# Upper bound on the per-tune capture length. Songs that hit an ``End``
# in their sequence (flashitback, euphoria) are captured for exactly
# their playthrough length; songs that loop forever (bronkosaurus,
# rain8580) are capped here. Override via env for a longer/shorter run.
MAX_SONG_FRAMES = int(os.environ.get("SIDWIZARD_MAX_SONG_FRAMES", "20000"))

# Use a non-default binmon port so concurrent agents driving their own
# VICE containers on :6502 don't collide. Override via env if you have
# something on :6612 already.
DEFAULT_PORT = int(os.environ.get("SIDWIZARD_BINMON_PORT", "6612"))


def _docker_available() -> bool:
    try:
        out = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="requires a reachable Docker daemon"),
]


def _song_length_frames(swm_file: Path) -> int:
    """Number of player frames for a full playthrough of ``swm_file``.

    Runs pysidwizard's own player until it reports ``finished`` (the
    sequence hit an ``End``), capped at :data:`MAX_SONG_FRAMES` for
    songs that loop forever. The reference capture and the frame-by-
    frame diff both run for this many frames, so the *entire* song is
    verified rather than a fixed prefix.

    Reference-CSV frame numbers map 1:1 to ``play_frame()`` calls (true
    even for multispeed tunes like euphoria, whose IRQ-rate capture is
    one CSV row group per player tick), so this count is directly usable
    as sidwizard-driver's ``--frames`` argument.
    """
    player = SWMPlayer(read_swm(str(swm_file)))
    frames = 0
    while not player.finished and frames < MAX_SONG_FRAMES:
        player.play_frame()
        frames += 1
    return frames


def _run_capture(swm: Path, out_csv: Path, port: int, frames: int) -> None:
    """Drive ``sidwizard-driver.capture`` to produce a reference CSV.

    Goes through the CLI rather than calling ``main()`` directly so a
    sidwizard-driver crash leaves a clean process boundary (any leaked
    VICE container is the test's own pytest-level cleanup concern).
    """
    rc = subprocess.run(
        [
            sys.executable,
            "-m",
            "sidwizard_driver.capture",
            "--swm",
            str(swm),
            "--frames",
            str(frames),
            "--out",
            str(out_csv),
            "--port",
            str(port),
        ],
        check=False,
    ).returncode
    if rc != 0:
        raise RuntimeError(f"sidwizard_driver.capture failed (rc={rc}) for {swm.name}")


@pytest.mark.parametrize("tune", TUNE_NAMES)
def test_pysidwizard_matches_fresh_reference(tune: str, tmp_path: Path) -> None:
    """Every frame and every SID register that pysidwizard's player
    emits must agree with a fresh capture from the real binary. Uses
    the committed ghost CSV for the editor-residue seed (same as
    ``test_reference_progress_meter``)."""
    swm = swm_path(tune)
    fresh_ref = tmp_path / f"{tune}.reference.csv"
    frames = _song_length_frames(swm)
    _run_capture(swm, fresh_ref, DEFAULT_PORT, frames)

    ghost_path = FIXTURES / f"{tune}.ghost.csv"
    assert ghost_path.exists(), f"missing ghost dump fixture {ghost_path}"

    ref_rows = load_reference_csv(fresh_ref)
    result = diff_against_reference(
        swm,
        ref_rows,
        skip_transition=True,
        ghost_path=ghost_path,
    )
    assert result.matched, (
        f"{tune}: pysidwizard's output differs from a fresh capture "
        f"from real SID-Wizard. First divergence at frame "
        f"{result.first_divergence.frame}/{result.n_frames}; "  # type: ignore[union-attr]
        f"{result.divergent_register_writes} cell(s) differ."
    )


def _cleanup_lingering_containers() -> None:
    """Reap any ``asid-vice-*`` containers a previous failed run left
    behind on this runner. Best-effort — never fails the test."""
    try:
        out = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=asid-vice-"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        ids = [i for i in out.stdout.split() if i]
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], check=False, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def setup_module(_module) -> None:  # noqa: D401
    """Module-level cleanup hook."""
    _cleanup_lingering_containers()
