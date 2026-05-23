"""Per-tick conformance ratchet against committed ghost dumps.

For each fixture tune we seed pysidwizard from a real SID-Wizard
ghost-register frame K, run one PLAYER tick, and compare against
ghost K+1. The ``conformance_baseline.json`` fixture records the
per-(tune, voice, var) miss counts at the last green run; this test
fails if any cell regresses.

The point of this test is to give every player-feature change a sharp,
unambiguous signal: cell-diff conflates everything, but the conformance
scorecard isolates per-feature transitions. When a feature lands cleanly
its (var, voice, tune) cells drop misses; when one regresses, the test
points directly at the broken variable.

Regenerating the baseline (after intentional improvements)::

    PYTHONPATH=src python3 tools/conformance.py \
        --out tests/fixtures/conformance_baseline.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
FIXTURES = Path(__file__).parent / "fixtures"

sys.path.insert(0, str(TOOLS))
from conformance import (  # noqa: E402  # type: ignore[import-not-found]
    TUNES,
    ZP_NAMES,
    conform_tune,
)

BASELINE_PATH = FIXTURES / "conformance_baseline.json"


@pytest.fixture(scope="module")
def baseline() -> dict:
    """Load the committed baseline scorecard, indexed by tune."""
    with open(BASELINE_PATH) as fp:
        raw = json.load(fp)
    return {entry["tune"]: entry for entry in raw}


@pytest.fixture(scope="module")
def current() -> dict:
    """Run the conformance harness on every fixture tune once per session."""
    from tests._swm_cache import swm_path as fetch_swm

    out: dict = {}
    for tune in TUNES:
        ghost_path = FIXTURES / f"{tune}.ghost.csv"
        out[tune] = conform_tune(
            tune=tune,
            swm_path=fetch_swm(tune),
            ghost_path=ghost_path,
            start_frame=1,
            max_frames=None,
            report_all=False,
        )
    return out


@pytest.mark.parametrize("tune", list(TUNES))
def test_no_var_regresses_from_baseline(tune, baseline, current, capsys):
    """No (voice, var) cell may have MORE misses than its baseline."""
    base = baseline[tune]["voices"]
    now = current[tune]["voices"]
    regressions: list[tuple[int, str, int, int]] = []
    improvements: list[tuple[int, str, int, int]] = []
    for vi_str, base_vars in base.items():
        vi = int(vi_str)
        # current's keys are real ints; baseline JSON loads them as strs.
        now_vars = now[vi]
        for name in ZP_NAMES.values():
            base_misses = base_vars[name]["misses"]
            now_misses = now_vars[name]["misses"]
            if now_misses > base_misses:
                regressions.append((vi, name, base_misses, now_misses))
            elif now_misses < base_misses:
                improvements.append((vi, name, base_misses, now_misses))

    with capsys.disabled():
        print()
        total_now = sum(rec["misses"] for vd in now.values() for rec in vd.values())
        total_base = sum(rec["misses"] for vd in base.values() for rec in vd.values())
        print(f"[{tune}] misses now={total_now} baseline={total_base}")
        for vi, name, b, n in improvements:
            print(f"  IMPROVED v{vi} {name}: {b} -> {n}")
        for vi, name, b, n in regressions:
            print(f"  REGRESSED v{vi} {name}: {b} -> {n}")

    assert not regressions, (
        f"{tune}: {len(regressions)} (voice, var) cell(s) regressed; "
        f"see captured output for the list. If the regression is "
        f"intentional, regenerate the baseline with "
        f"`PYTHONPATH=src python3 tools/conformance.py "
        f"--out tests/fixtures/conformance_baseline.json`."
    )
