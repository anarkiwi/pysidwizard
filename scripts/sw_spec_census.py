"""Run :mod:`sw_spec_diff` over many SID-Wizard tunes in parallel, one line per tune.

    python scripts/sw_spec_census.py [TUNE.sid ...] [--frames N] [--jobs J]

Without tunes: the 1.97 example exports and the oracle tunes the binder binds.
"""

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from pysidtracker.testing import song_frames
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sw_bind_census  # noqa: E402
import sw_spec_diff  # noqa: E402

for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_var, "2")


def _frames(data: bytes, frames) -> int:
    if frames:
        return frames
    try:
        return song_frames(data, 1)
    except Exception:  # pylint: disable=broad-except
        return 3000


def run(name: str, path: Path, frames) -> str:
    """One tune's result line."""
    data = path.read_bytes()
    try:
        sw_bind_census.bind(path)
    except Exception as exc:  # pylint: disable=broad-except
        return f"{name}: unbound: {exc}"
    count = _frames(data, frames)
    try:
        found = sw_spec_diff.first_difference(data, count, progress=False)
    except Exception as exc:  # pylint: disable=broad-except
        return f"{name}: error: {type(exc).__name__}: {exc}"
    if found is None:
        return f"{name}: {count} calls equal"
    frame, diff, _players = found
    return f"{name}: call {frame} differs at {[hex(a) for a in diff[:8]]}"


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tunes", nargs="*", type=Path)
    parser.add_argument("--frames", type=int)
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    args = parser.parse_args(argv)
    tunes = {p.stem: p for p in args.tunes} or sw_bind_census.default_tunes()
    with ProcessPoolExecutor(args.jobs) as pool:
        futures = [pool.submit(run, n, p, args.frames) for n, p in tunes.items()]
        lines = [f.result() for f in tqdm(as_completed(futures), total=len(futures), unit="tune")]
    for line in sorted(lines):
        print(line)


if __name__ == "__main__":
    main()
