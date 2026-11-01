"""Microseconds per play call of a playspec on tunes, native and Python, and the time to render
a whole tune.

    python scripts/spec_bench.py SPEC.play [SPEC.play ...] --tunes TUNE.sid ... [--frames N]
"""

import argparse
import sys
import time
from pathlib import Path

from pysidtracker.playspec import load
from pysidtracker.testing import song_frames

sys.path.insert(0, str(Path(__file__).resolve().parent))

from spec_compare import make  # noqa: E402  # pylint: disable=wrong-import-position


def per_frame(spec, data: bytes, frames: int, native: bool) -> float:
    """Microseconds per call after a warm-up of ten calls."""
    player = make(spec, data, 1, native)
    for _ in range(10):
        player.play_frame()
    start = time.perf_counter()
    for _ in range(frames):
        player.play_frame()
    return 1e6 * (time.perf_counter() - start) / frames


def main() -> None:
    """Parse arguments and print one line per tune and spec."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("specs", nargs="+", type=Path)
    parser.add_argument("--tunes", nargs="+", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=2000)
    args = parser.parse_args()
    for tune in args.tunes:
        data = tune.read_bytes()
        whole = song_frames(data, 1)
        for path in args.specs:
            spec = load(str(path))
            native = per_frame(spec, data, args.frames, True)
            python = per_frame(spec, data, args.frames, False)
            start = time.perf_counter()
            player = make(spec, data, 1, True)
            for _ in range(whole):
                player.play_frame()
            print(
                f"{tune.name[:30]:30} {path.name:14} native {native:7.1f}us python {python:8.1f}us"
                f" whole tune {whole} frames {time.perf_counter() - start:.2f}s",
                flush=True,
            )


if __name__ == "__main__":
    main()
