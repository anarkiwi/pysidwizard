"""Trace a playspec translation validation: where the symbolic 6502 spends its steps.

    python scripts/sw_tv_trace.py TUNE.sid ROUTINE LABEL [--offset N] [--x N] [--stop LABEL]

Runs ``tv.validate`` with a tqdm progress over 6502 steps; prints the pcs stepped most.
"""

import argparse
import signal
import sys
from collections import Counter
from pathlib import Path

from pysidtracker import SidImage
from pysidtracker.disasm import image_after_init
from pysidtracker.playspec import tv
from tqdm import tqdm

from pysidwizard import playspec


class _Stop(Exception):
    pass


def main(argv=None) -> None:
    """Parse ``argv`` and run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tune", type=Path)
    parser.add_argument("routine")
    parser.add_argument("label")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--x", type=int, default=0)
    parser.add_argument("--stop")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args(argv)
    view = bytes(image_after_init(SidImage.from_sid(args.tune.read_bytes())).window(0, 0x10000))
    found = playspec.binding(view)
    pcs: Counter = Counter()
    progress = tqdm(unit="step")
    step = tv.Machine6502.step

    def traced(self, path):
        pcs[path.pc] += 1
        progress.update()
        return step(self, path)

    def alarm(*_):
        raise _Stop

    tv.Machine6502.step = traced
    signal.signal(signal.SIGALRM, alarm)
    signal.alarm(args.seconds)
    try:
        result = tv.validate(
            playspec.SPEC,
            found,
            args.routine,
            view,
            found[args.label] + args.offset,
            registers={"X": args.x},
            preserve=("X",) if args.routine.startswith("voice") else (),
            stop=found[args.stop] if args.stop else None,
        )
        print(result)
    except _Stop:
        print(f"stopped after {args.seconds}s", file=sys.stderr)
    finally:
        progress.close()
        names = {v: k for k, v in found.items() if isinstance(v, int) and not isinstance(v, bool)}
        for pc, count in pcs.most_common(args.top):
            print(f"${pc:04x} {names.get(pc, ''):16} {count}")


if __name__ == "__main__":
    main()
