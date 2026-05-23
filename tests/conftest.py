"""Shared fixtures for the pysidwizard test suite.

SWM test tunes are NOT tracked in the repo — they're SID-Wizard binary
artifacts redistributed as part of the SID-Wizard 1.94 source release.
``tests._swm_cache.swm_path`` fetches + verifies them on demand
(delegating the download to ``sidwizard-driver``). Tests should never
hard-code ``tests/data/*.swm`` paths; use the ``sample_path`` or
``swm_paths`` fixtures below.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Allow running pytest without `pip install -e .` by injecting src/ on path.
_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tests._swm_cache import TUNE_NAMES, all_swm_paths, swm_path  # noqa: E402


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
