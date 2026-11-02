"""Record or check the spec's per-call SID output over a survey's tunes in two concurrent chunks.

``spec_gate.py record|check REF_PREFIX SPEC.play TUNES.csv --trace PATH/spec_trace.py``
runs pysidtracker's ``tools/spec_trace.py`` over each half of the survey (paths in the CSV are
relative to ``--root``) and exits with the number of tunes that differ.
"""

import argparse
import csv
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CHUNKS = 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("mode", choices=("record", "check"))
    parser.add_argument("ref", help="prefix of the per-chunk reference files")
    parser.add_argument("spec")
    parser.add_argument("tunes")
    parser.add_argument("--trace", required=True, help="pysidtracker's tools/spec_trace.py")
    parser.add_argument("--root", default="/scratch/datasets/hvsc/C64Music")
    parser.add_argument("--frames", type=int, default=3000)
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    with open(args.tunes, encoding="utf-8") as handle:
        paths = [str(Path(args.root, row["path"])) for row in csv.DictReader(handle)]
    env = {**os.environ}
    env.update(OMP_NUM_THREADS="2", MKL_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2")
    with tempfile.TemporaryDirectory() as tmp:
        procs = []
        for k in range(CHUNKS):
            chunk = Path(tmp, f"tunes{k}.csv")
            chunk.write_text("path\n" + "".join(p + "\n" for p in paths[k::CHUNKS]))
            cmd = [sys.executable, args.trace, args.mode, f"{args.ref}{k}.pkl.gz"]
            cmd += [str(Path(args.spec).resolve()), str(chunk)]
            cmd += ["--frames", str(args.frames), "--jobs", str(args.jobs)]
            procs.append(subprocess.Popen(cmd, env=env))
        return sum(p.wait() for p in procs)


if __name__ == "__main__":
    sys.exit(main())
