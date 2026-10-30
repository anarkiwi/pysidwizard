"""Prove the SID-Wizard playspec's routines against tunes' 6502 code in parallel.

    python scripts/sw_proof_census.py [NAME ...] [--jobs J]

NAMEs are 1.97 example exports or oracle tune ids (default: the default suite's exports).
"""

import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from pysidtracker.testing import resolve_tune
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pysidwizard import playspec  # noqa: E402  # pylint: disable=wrong-import-position
from tests import test_playspec as T  # noqa: E402  # pylint: disable=wrong-import-position

for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_var, "2")


def view_of(name: str) -> bytes:
    """The post-init memory of an export or oracle tune."""
    # pylint: disable=protected-access
    if name in T.ALL_EXPORTS:
        return T._view(T._export(name))
    return T._view(Path(resolve_tune(T.TUNES[name])).read_bytes())


def prove(name: str, index: int) -> str:
    """The result line of routine ``index`` of tune ``name``."""
    view = view_of(name)
    found = playspec.binding(view)
    label, routine, pc, stop, cases = T.routines(found, view)[index]
    start = time.time()
    for assume in cases:
        result = T._prove(  # pylint: disable=protected-access
            view, found, routine, pc, stop=stop, assume=assume
        )
        if not result.proven:
            cex = result.counterexample
            shown = {k: v for k, v in cex.items() if k not in ("memory", "cells")}
            return f"{name} {label}: counterexample {shown}"
    return f"{name} {label}: proven in {time.time() - start:.1f}s"


def main(argv=None) -> None:
    """Parse ``argv`` and run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*")
    parser.add_argument("--only", action="append", help="a routine label to prove (repeatable)")
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    args = parser.parse_args(argv)
    jobs = []
    for name in args.names or list(T.PROOF_EXPORTS):
        view = view_of(name)
        found = playspec.binding(view)
        jobs += [
            (name, i)
            for i, r in enumerate(T.routines(found, view))
            if not args.only or r[0].split()[0] in args.only
        ]
    proven = 0
    with ProcessPoolExecutor(args.jobs) as pool:
        futures = [pool.submit(prove, n, i) for n, i in jobs]
        for future in tqdm(as_completed(futures), total=len(futures), unit="routine"):
            line = future.result()
            proven += ": proven" in line
            if ": proven" not in line:
                tqdm.write(line)
    print(f"{proven}/{len(jobs)} proven")


if __name__ == "__main__":
    main()
