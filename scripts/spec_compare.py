"""Play tunes with two playspecs (and optionally the tune's own 6502) call by call and report
the first call whose SID writes, registers or memory (outside the stack page) differ.

    python scripts/spec_compare.py OLD.play [NEW.play] TUNES.csv|TUNE.sid ... [--frames N]
        [--native|--python] [--oracle] [--jobs J]

``NEW.play`` defaults to the packaged spec; a CSV needs a ``path`` column and ``--player`` selects
its rows by the ``player`` column. Prints one line per tune: frames played, microseconds per
frame of each spec, and the first difference. Exit status is the number of differing tunes.
"""

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")

from pysidtracker import SidImage  # noqa: E402
from pysidtracker.errors import SidError  # noqa: E402
from pysidtracker.header import parse_sid_header  # noqa: E402
from pysidtracker.oracle import EmuPlayer  # noqa: E402
from pysidtracker.playspec import SpecPlayer, load  # noqa: E402
from pysidtracker.testing import song_frames  # noqa: E402
from tqdm import tqdm  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sw_spec_diff import STACK, play_entry  # noqa: E402  # pylint: disable=wrong-import-position
from pysidwizard import playspec  # noqa: E402  # pylint: disable=wrong-import-position


def make(spec, data: bytes, song: int, native: bool):
    """A SpecPlayer of ``spec`` on the SID ``data``."""
    image = SidImage.from_bytes(data)
    header = parse_sid_header(data)
    play = header.play_address or play_entry(image)
    return SpecPlayer(
        spec,
        image.image,
        image.load,
        header.init_address,
        play,
        song - 1,
        bind=playspec.binder(),
        entries=playspec.entries,
        psid=header.is_psid or not header.play_address,
        native=native,
    )


def compare(job):
    """``(path, frames, per-frame microseconds by player, difference or None)`` of one tune."""
    path, old, new, frames, native, oracle = job
    data = Path(path).read_bytes()
    specs = [load(old), load(new)]
    try:
        count = frames or min(song_frames(data, 1), 6000)
        players = [make(s, data, 1, native) for s in specs]
    except (ValueError, SidError) as err:
        return path, 0, [], f"refused: {err}"
    if oracle:
        players.append(EmuPlayer(*_emu_args(data)))
    spent = [0.0] * len(players)
    for frame in range(count):
        try:
            for k, player in enumerate(players):
                t = time.perf_counter()
                player.play_frame()
                spent[k] += time.perf_counter() - t
        except (ValueError, SidError) as err:
            return path, 0, [], f"refused: {err}"
        base = players[0]
        for k, player in enumerate(players[1:], 1):
            what = _differs(base, player, k == 2)
            if what:
                return path, frame, [], f"frame {frame}: player {k} {what}"
    return path, count, [1e6 * s / max(count, 1) for s in spent], None


def _emu_args(data: bytes):
    image = SidImage.from_bytes(data)
    header = parse_sid_header(data)
    return (
        image.image,
        image.load,
        header.init_address,
        header.play_address or play_entry(image),
        0,
    )


def _differs(a, b, emu: bool) -> str:
    if not emu and a.writes != b.writes:
        return f"writes {a.writes[:6]} vs {b.writes[:6]}"
    if a.regs != b.regs:
        return "registers"
    mine = a.memory
    theirs = bytes(b._mem) if emu else b.memory  # pylint: disable=protected-access
    for addr in range(0x10000):
        if mine[addr] != theirs[addr] and addr not in STACK:
            return f"memory ${addr:04x} {mine[addr]:02x} vs {theirs[addr]:02x}"
    return ""


def tunes(args):
    """The tune paths named by ``args.tunes``."""
    for item in args.tunes:
        if item.suffix == ".csv":
            with open(item, encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    if not args.player or row.get("player") == args.player:
                        yield Path(row["path"])
        else:
            yield item


def main(argv=None) -> int:
    """Parse ``argv``, compare, print."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old", type=Path)
    parser.add_argument("tunes", nargs="+", type=Path)
    parser.add_argument("--new", type=Path)
    parser.add_argument("--frames", type=int, default=0)
    parser.add_argument("--player", default="")
    parser.add_argument("--python", action="store_true")
    parser.add_argument("--oracle", action="store_true")
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args(argv)
    new = args.new or Path(playspec.__file__).parent / "specs" / "sidwizard.play"
    jobs = [(str(t), args.old, new, args.frames, not args.python, args.oracle) for t in tunes(args)]
    bad = 0
    with ProcessPoolExecutor(args.jobs) as pool:
        for path, frames, us, err in tqdm(pool.map(compare, jobs), total=len(jobs), unit="tune"):
            bad += err is not None and not err.startswith("refused")
            speed = " ".join(f"{u:.0f}us" for u in us)
            print(f"{Path(path).name[:40]:40} {frames:5} {speed:30} {err or 'same'}", flush=True)
    return bad


if __name__ == "__main__":
    sys.exit(main())
