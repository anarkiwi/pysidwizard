"""Classify the not-decompiled SID-Wizard subtunes of an HVSC ``tunes.csv`` (#52).

A ``not_decompiled`` verdict is exactly a ``SidParseError`` from
``parse_sid_song(data, song)``, so this reproduces that bucket without sidtrace.
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from pysidtracker import SidParseError

from pysidwizard import parse_sid_song

# Case-insensitive match on the SIDId player name (the Hermit/SidWizard group).
PLAYER = "SidWizard"

OK = "ok"
NOT_DECOMPILED = "not_decompiled"
LIBRARY_ERROR = "library_error"


@dataclass(frozen=True)
class Result:
    """One ``(tune, song)`` classification row."""

    path: str
    song: int
    verdict: str
    error: str


def _is_player(name: str, player: str) -> bool:
    """Whether a SIDId player name matches ``player`` (case-insensitive regex)."""
    return bool(re.search(player, name, re.I))


def songs(tune: Dict) -> List[int]:
    """The 1-based subtunes of one ``find`` CSV row (``songs`` is the count)."""
    return list(range(1, int(tune["songs"]) + 1))


def classify(data: bytes, song: int) -> Tuple[str, str]:
    """``(verdict, error)`` of ``parse_sid_song(data, song)`` on raw bytes.

    A ``SidParseError`` is the survey's ``not_decompiled`` verdict; success is
    ``ok``; any other exception is ``library_error`` (a real bug).
    """
    try:
        parse_sid_song(data, song)
    except SidParseError as exc:
        return NOT_DECOMPILED, f"{type(exc).__name__}: {exc}".strip()
    except Exception as exc:  # pylint: disable=broad-except  # any non-scope failure
        return LIBRARY_ERROR, f"{type(exc).__name__}: {exc}".strip()
    return OK, ""


def load_tunes(csv_path: Path) -> List[Dict]:
    """The rows of a survey ``find`` CSV (``path,md5,player,songs,clock,lengths``)."""
    with open(csv_path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def tune_rows(hvsc: Path, player: str, tune: Dict) -> List[Result]:
    """Classify every subtune of one ``find`` row (empty if the player no-match)."""
    if not _is_player(tune["player"], player):
        return []
    path = hvsc / tune["path"]
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    return [Result(tune["path"], song, *classify(data, song)) for song in songs(tune)]


def _tune_worker(args: Tuple) -> List[Result]:
    """Picklable pool target unpacking ``(hvsc, player, tune)`` for :func:`tune_rows`."""
    return tune_rows(*args)


def not_decompiled(
    hvsc: Path,
    csv_path: Path,
    player: str = PLAYER,
    jobs: Optional[int] = None,
) -> List[Result]:
    """Every non-``ok`` ``(tune, song)`` row of the ``find`` CSV under ``hvsc``.

    ``jobs`` is 1 (serial, pool-free -- robust under xdist workers) or the
    default pool size (``min(8, cpus)``) for real runs.
    """
    tunes = [t for t in load_tunes(csv_path) if _is_player(t["player"], player)]
    if not jobs or jobs > 1:
        jobs = jobs or min(8, (mp.cpu_count() or 1))
        ctx = mp.get_context("spawn")
        with ctx.Pool(jobs) as pool:
            rows = [
                r
                for chunk in pool.imap_unordered(
                    _tune_worker, [(hvsc, player, t) for t in tunes], chunksize=4
                )
                for r in chunk
            ]
    else:
        rows = [r for t in tunes for r in tune_rows(hvsc, player, t)]
    return sorted((r for r in rows if r.verdict != OK), key=lambda r: (r.path, r.song))


def write_csv(rows: Iterable[Result], out: Path) -> None:
    """Write ``rows`` as a ``path,song,verdict,error`` CSV."""
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "song", "verdict", "error"])
        for r in rows:
            w.writerow([r.path, r.song, r.verdict, r.error])


def main(argv: Optional[List[str]] = None) -> int:
    """CLI: print the not-decompiled subtunes of a ``find`` CSV (1 if any, else 0)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("hvsc", help="HVSC root (C64Music) the CSV paths resolve under")
    parser.add_argument("tunes", help="a survey ``find`` CSV")
    parser.add_argument("--player", default=PLAYER, help="case-insensitive player regex")
    parser.add_argument("--song", type=int, help="scope to one 1-based subtune")
    parser.add_argument("--tune", help="scope to one CSV path")
    parser.add_argument("-j", "--jobs", type=int, default=0, help="worker processes")
    parser.add_argument("--out", help="also write results.csv to this path")
    args = parser.parse_args(argv)

    rows = not_decompiled(Path(args.hvsc), Path(args.tunes), args.player, args.jobs or None)
    if args.tune:
        rows = [r for r in rows if r.path == args.tune]
    if args.song:
        rows = [r for r in rows if r.song == args.song]

    by_verdict = {}
    for r in rows:
        by_verdict[r.verdict] = by_verdict.get(r.verdict, 0) + 1
    print(f"not-ok subtunes: {len(rows)}   by verdict: {by_verdict}")
    for r in rows:
        print(f"{r.verdict:16s} {r.path}  song {r.song}  {r.error[:90]}")

    if args.out:
        write_csv(rows, args.out)
    return 1 if rows else 0


if __name__ == "__main__":
    sys.exit(main())
