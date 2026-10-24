"""Tabulate code-detected player features against each tune's header build.

``python scripts/feature_census.py tunes.txt [--csv out.csv] [--jobs N]`` reads one
``.sid`` path per line and prints, per flag, the tunes whose detection disagrees with
the build their SWM header's driver byte names; ``--csv`` also lists each tune's
release quirks (:func:`~pysidwizard.features.detect_quirks`).
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, fields
from pathlib import Path

from tqdm import tqdm

from pysidwizard.constants import DRIVER_TYPE_POS, SWM_MAGIC
from pysidwizard.features import (
    BUILD_FLAGS,
    DETECTED_FLAGS,
    PlayerQuirks,
    detect_flags,
    detect_quirks,
)
from pysidwizard.sidfile import parse_psid_header


def census_one(path: str):
    """``(path, header driver or None, {flag: detected}, {quirk: detected})``, or None."""
    try:
        data = Path(path).read_bytes()
        header = parse_psid_header(data)
    except Exception:  # pylint: disable=broad-except
        return None
    pos = data.find(SWM_MAGIC, header.data_start)
    driver = data[pos + DRIVER_TYPE_POS] if pos >= 0 else None
    code = data[header.data_start :]
    return (
        path,
        driver,
        detect_flags(code, header.real_load_address),
        asdict(detect_quirks(code, data)),
    )


def main() -> None:
    """Run the census over the listed tunes."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tunes", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--examples", type=int, default=3)
    args = parser.parse_args()
    paths = [p for p in args.tunes.read_text().splitlines() if p]
    with ProcessPoolExecutor(args.jobs) as pool:
        mapped = pool.map(census_one, paths, chunksize=16)
        rows = [r for r in tqdm(mapped, total=len(paths), desc="tunes") if r]
    disagree, examples, headerless = Counter(), defaultdict(list), 0
    for path, driver, found, _ in rows:
        if driver not in BUILD_FLAGS:
            headerless += 1
            continue
        for flag, value in found.items():
            if getattr(BUILD_FLAGS[driver], flag) != value:
                disagree[flag, driver, value] += 1
                examples[flag, driver, value].append(path)
    print(f"{len(rows)} tunes, {headerless} without a known header driver")
    for (flag, driver, value), count in sorted(disagree.items()):
        print(f"{flag:16} driver {driver} detected {value!s:5} x{count}")
        for path in examples[flag, driver, value][: args.examples]:
            print(f"    {path}")
    if args.csv:
        with args.csv.open("w", newline="") as fh:
            out = csv.writer(fh)
            quirks = [f.name for f in fields(PlayerQuirks)]
            out.writerow(["path", "driver", *DETECTED_FLAGS, *quirks])
            for path, driver, found, quirk in rows:
                flags = (found.get(f, "") for f in DETECTED_FLAGS)
                out.writerow([path, driver, *flags, *(quirk[q] for q in quirks)])


if __name__ == "__main__":
    main()
