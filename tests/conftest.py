"""Shared fixtures for the pysidwizard test suite.

SWM test tunes are NOT tracked in the repo — they're SID-Wizard binary
artifacts redistributed as part of the SID-Wizard 1.94 source release.
``tests._swm_cache.swm_path`` downloads the four SWM tunes directly from
the SID-Wizard 1.94 tarball and SHA-256-verifies each on demand. Tests
should never hard-code ``tests/data/*.swm`` paths; use the ``sample_path``
or ``swm_paths`` fixtures below.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Tuple

import pytest

# Allow running pytest without `pip install -e .` by injecting src/ on path.
_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tests._swi_cache import (  # noqa: E402
    SWI_NAMES,
    nepzenek_path,
    swi_path,
)
from tests._swm_cache import (  # noqa: E402
    MULTI_SID_NAMES,
    OOB_INST_TUNE_NAMES,
    TUNE_NAMES,
    all_swm_paths,
    multi_sid_path,
    oob_inst_path,
    p00_pair,
    swm_path,
)


def pytest_addoption(parser, pluginmanager):
    """Keep ``-p no:xdist`` usable for serial debugging.

    ``-n auto`` lives in ``addopts`` (see pyproject) so the suite
    parallelises by default. When xdist is explicitly disabled via
    ``-p no:xdist``, that option is no longer registered and pytest would
    abort on the unknown ``-n`` argument. Re-register a no-op ``-n`` in
    that case so the run simply proceeds serially (the value is ignored —
    without xdist there are no workers to spawn).
    """
    if not pluginmanager.hasplugin("xdist"):
        group = parser.getgroup("xdist-disabled-fallback")
        # ``-n`` is a reserved lowercase short option; use the same private
        # entry point xdist itself uses to register it.
        group._addoption(
            "-n",
            "--numprocesses",
            dest="numprocesses",
            default=None,
            help="(ignored: pytest-xdist is disabled — running serially)",
        )


@pytest.fixture(scope="session")
def swm_paths() -> dict:
    """Cached ``{tune: Path}`` for the four reference tunes. Materialised
    once per session, downloaded + SHA-256 verified on first call."""
    return all_swm_paths()


@pytest.fixture(params=TUNE_NAMES)
def sample_path(request) -> Path:
    """A real SWM sample from the SID-Wizard 1.94 distribution.
    Fetched + verified on demand."""
    return swm_path(request.param)


@pytest.fixture
def sample_bytes(sample_path: Path) -> bytes:
    return sample_path.read_bytes()


@pytest.fixture(params=OOB_INST_TUNE_NAMES)
def oob_sample_path(request) -> Path:
    """An issue #57 module whose rows reference an instrument above the count."""
    return oob_inst_path(request.param)


@pytest.fixture
def oob_sample_bytes(oob_sample_path: Path) -> bytes:
    return oob_sample_path.read_bytes()


@pytest.fixture(params=MULTI_SID_NAMES)
def multi_sample_path(request) -> Path:
    """A native multi-SID work file (SWMS/SWT/SWMQ), fetched + verified."""
    return multi_sid_path(request.param)


@pytest.fixture(params=SWI_NAMES)
def swi_file(request) -> Path:
    """A curated ``.swi`` single-instrument fixture (issue #54), SHA-256 verified."""
    return swi_path(request.param)


@pytest.fixture
def swi_bytes(swi_file: Path) -> bytes:
    """Raw bytes of a curated ``.swi`` fixture (issue #54)."""
    return swi_file.read_bytes()


@pytest.fixture(scope="session")
def nepzenek_file() -> Path:
    """The SID-Wizard 1.97 ``nepzenek.swm`` whose ``CIMBALOM`` instrument matches
    the curated ``cimbalom.swi`` fixture (issue #54 cross-check)."""
    return nepzenek_path()


@pytest.fixture
def multi_sample_bytes(multi_sample_path: Path) -> bytes:
    return multi_sample_path.read_bytes()


@pytest.fixture
def p00_pair_file() -> Tuple[Path, Path]:
    """The 1.4 quickscore module as a PC64 ``.P00`` container and its ``.prg``
    twin (issue #61); both SHA-256 verified."""
    return p00_pair()
