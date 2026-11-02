"""List the tunes of a CSV that a spec does not play entirely on the native core.

``sw_native_check.py SPEC.play TUNES.csv [--frames N]``.
"""

import argparse
import csv
import sys
from concurrent.futures import ProcessPoolExecutor

from sw_state_diff import player
from tqdm import tqdm


def check(job):
    """``(path, native)``: whether the tune plays natively, or an error text."""
    path, spec, frames = job
    try:
        tune = player(path, spec)
        for _ in range(frames):
            tune.play_frame()
        # pylint: disable-next=protected-access
        return path, tune._native_ok() and tune._bails == 0 and tune._fast
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return path, f"{type(exc).__name__}: {str(exc)[:80]}"


def main() -> int:
    """Print the tunes that do not run natively."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("spec")
    parser.add_argument("tunes")
    parser.add_argument("--frames", type=int, default=20)
    parser.add_argument("--jobs", type=int, default=16)
    args = parser.parse_args()
    with open(args.tunes, encoding="utf-8") as handle:
        paths = [row["path"] for row in csv.DictReader(handle)]
    with ProcessPoolExecutor(args.jobs) as pool:
        rows = list(
            tqdm(pool.map(check, [(p, args.spec, args.frames) for p in paths]), total=len(paths))
        )
    bad = [(p, r) for p, r in rows if r is not True]
    for path, why in bad:
        print(path, why)
    print("not native", len(bad), "of", len(rows))
    return len(bad)


if __name__ == "__main__":
    sys.exit(main())
