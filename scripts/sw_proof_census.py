"""Prove the SID-Wizard playspec's routines against tunes' 6502 code in parallel.

NAMEs are 1.97 example exports or oracle tune ids (default: the default suite's exports).
``--seeds S`` reports each job's slowest proof under z3 seeds 0..S-1; ``--jobs 1`` proves
in this process. The exit status is 1 unless every job is proven.
"""

import argparse
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import z3
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


def _solver_settings(seed: int, timeout: int) -> None:
    for param in ("smt.random_seed", "sat.random_seed"):
        z3.set_param(param, seed)
    z3.set_param("timeout", timeout * 1000 or 4294967295)


def _shown(cex: dict) -> dict:
    return {k: v for k, v in cex.items() if k not in ("memory", "cells")}


def prove(name: str, index: int, pick: tuple = (0, 1), seed: int = 0, timeout: int = 0) -> str:
    """The result line of routine ``index`` of tune ``name`` over its cases
    ``pick = (first, step)``, under z3 random ``seed``, each solver check limited to
    ``timeout`` seconds (0: none)."""
    _solver_settings(seed, timeout)
    view = view_of(name)
    found = playspec.binding(view)
    label, routine, pc, stop, cases = T.routines(found, view)[index]
    if pick != (0, 1):
        label, cases = f"{label} cases {pick[0]}::{pick[1]}", cases[pick[0] :: pick[1]]
    start = time.time()
    for assume in cases:
        try:
            result = T._prove(  # pylint: disable=protected-access
                view, found, routine, pc, stop=stop, assume=assume
            )
        except z3.Z3Exception:
            return f"{name} {label}: unknown after {time.time() - start:.1f}s (seed {seed})"
        if not result.proven:
            return f"{name} {label}: counterexample {_shown(result.counterexample)}"
    return f"{name} {label}: proven in {time.time() - start:.1f}s"


def slowest(lines) -> dict:
    """The slowest proof time of each ``name label`` among proven result ``lines``."""
    out: dict = defaultdict(float)
    for line in lines:
        key, _, rest = line.partition(": proven in ")
        if rest:
            out[key] = max(out[key], float(rest.rstrip("s")))
    return dict(out)


def _jobs(args) -> list:
    shard = tuple(map(int, args.shard.split("/"))) if args.shard else (0, 1)
    jobs = []
    for name in args.names or list(T.PROOF_EXPORTS):
        view = view_of(name)
        for i, r in enumerate(T.routines(playspec.binding(view), view)):
            if args.only and r[0].split()[0] not in args.only:
                continue
            picks = [(c, len(r[4])) for c in range(len(r[4]))] if args.cases else [shard]
            jobs += [(name, i, p, seed, args.timeout) for p in picks for seed in range(args.seeds)]
    return jobs


def main(argv=None) -> int:
    """Parse ``argv`` and run; 0 when every job is proven."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*")
    parser.add_argument("--only", action="append", help="a routine label to prove (repeatable)")
    parser.add_argument("--cases", action="store_true", help="each case of a routine apart")
    parser.add_argument("--shard", help="K/N: only a routine's cases K, K+N, ...")
    parser.add_argument("--seeds", type=int, default=1, help="z3 random seeds per job")
    parser.add_argument("--timeout", type=int, default=0, help="seconds per solver check")
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    args = parser.parse_args(argv)
    jobs = _jobs(args)
    lines: list = []

    def report(results) -> None:
        for line in tqdm(results, total=len(jobs), unit="proof"):
            lines.append(line)
            if ": proven" not in line:
                tqdm.write(line)

    if args.jobs == 1:
        report(prove(*job) for job in jobs)
    else:
        with ProcessPoolExecutor(args.jobs) as pool:
            report(f.result() for f in as_completed([pool.submit(prove, *j) for j in jobs]))
    for key, worst in sorted(slowest(lines).items(), key=lambda kv: -kv[1]):
        print(f"{key}: slowest {worst:.1f}s")
    proven = sum(": proven" in line for line in lines)
    print(f"{proven}/{len(jobs)} proven")
    return int(proven != len(jobs) or not jobs)


if __name__ == "__main__":
    sys.exit(main())
