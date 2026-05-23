#!/usr/bin/env bash
# Regenerate tests/fixtures/{tune}.ghost.csv at 1500 frames each.
#
# Existing ghost CSVs are 500 frames; Phase 3 cascade can't reach K=0 on
# cells past frame 499. With 1500-frame ghosts, all 8 cluster-B cells
# become K=0 reachable. Run this once when sidwizard-driver / asid-vice
# are available; takes ~2 min/tune under warp.
set -euo pipefail

FRAMES=${FRAMES:-1500}
# Default to a non-default binmon port so concurrent agents driving
# their own pydefmon containers on :6502 don't conflict. Override
# with PORT=<n> if needed.
PORT=${PORT:-6612}
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
FIX="$REPO_ROOT/tests/fixtures"

# Fetch the SID-Wizard editor .d64 via sidwizard-driver (downloads +
# verifies the source tarball on demand, idempotent).
D64=$(python3 -c "from sidwizard_driver.fetch import fetch_disk1_d64; print(fetch_disk1_d64())")

# sidwizard-driver is consumed as the pip-installed package (currently
# v0.3.0+ — needs the FLTCTRL/FLTPOSI/CWEPCNT capture). Do NOT add a
# local checkout to PYTHONPATH; that masked the released package and
# produced regenerations against an unreleased tree.

for tune in flashitback bronkosaurus euphoria rain8580; do
    # Fetch (cache-hit fast path) the SWM from the SID-Wizard tarball.
    swm=$(PYTHONPATH="$REPO_ROOT" python3 -c "from tests._swm_cache import swm_path; print(swm_path('${tune}'))")
    out="$FIX/${tune}.ghost.csv"
    echo "=== ${tune} (${FRAMES} frames) ==="
    python3 -m sidwizard_driver.ghost_dump \
        --d64 "$D64" \
        --swm "$swm" \
        --frames "$FRAMES" \
        --out "$out" \
        --port "$PORT" \
        --annotate
done

echo "done. Verify with: wc -l $FIX/*.ghost.csv"
