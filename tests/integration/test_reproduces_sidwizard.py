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

This is the "every PR" enforceable-100%-reproduction gate. It's the
slow test (~3 min wall time per tune including the tarball download):
opt in with ``pytest -m integration tests/integration/``. CI runs it
on every push + PR via ``.github/workflows/integration.yml``.

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

from tests._diff_harness import diff_against_reference, load_reference_csv
from tests._swm_cache import TUNE_NAMES, swm_path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"

# Number of PAL frames to capture from real SID-Wizard. 1500 matches
# the committed ghost-CSV fixtures so the per-tune ghost seed picks
# up the same row-0 state.
REFERENCE_FRAMES = 1500

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
    _run_capture(swm, fresh_ref, DEFAULT_PORT, REFERENCE_FRAMES)

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
