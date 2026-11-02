"""Group tunes by the build flags the binder reads from their image.

``sw_builds.py TUNES.csv [--jobs J]`` prints each flag tuple with its tunes' count; ``--list`` also
the tunes, ``--csv PREFIX`` writes one tune list per build.
"""

import argparse
import collections
import csv
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from tqdm import tqdm


def flags(path):
    """``(path, {flag: bool}, {layout name: int})`` of a tune, or ``(path, error, None)``."""
    # pylint: disable=import-outside-toplevel
    from pysidtracker import find_format

    try:
        fmt = find_format(Path(path).read_bytes())
        player = fmt.spec_player(fmt.parser.parse(Path(path).read_bytes()))
        layout = player._bind(player.view(), player.entry)  # pylint: disable=protected-access
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return path, f"{type(exc).__name__}: {str(exc)[:80]}", None
    return (
        path,
        {k: v for k, v in layout.items() if isinstance(v, bool)},
        {k: v for k, v in layout.items() if isinstance(v, int) and not isinstance(v, bool)},
    )


def main() -> int:
    """Print the builds."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("tunes")
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--csv")
    args = parser.parse_args()
    with open(args.tunes, encoding="utf-8") as handle:
        paths = [row["path"] for row in csv.DictReader(handle)]
    with ProcessPoolExecutor(args.jobs) as pool:
        rows = list(tqdm(pool.map(flags, paths), total=len(paths), unit="tune"))
    groups = collections.defaultdict(list)
    for path, fl, _ in rows:
        groups[fl if isinstance(fl, str) else tuple(sorted(k for k, v in fl.items() if v))].append(
            path
        )
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    base = set(ordered[0][0]) if not isinstance(ordered[0][0], str) else set()
    for i, (key, tunes) in enumerate(ordered):
        if isinstance(key, str):
            print(i, len(tunes), "refused", key)
        else:
            print(i, len(tunes), "+", sorted(set(key) - base), "-", sorted(base - set(key)))
        if args.list:
            print("   ", *tunes, sep="\n    ")
        if args.csv:
            with open(f"{args.csv}{i}.csv", "w", encoding="utf-8") as out:
                out.write("path\n" + "\n".join(tunes) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
