"""List the HVSC songs SWMPlayer refuses within its first CALLS play calls, as the tests do.

    python scripts/sw_refusals.py --hvsc $HVSC LIST [--jobs N]

LIST holds one HVSC-relative ``.sid`` path per line; prints ``path<TAB>song<TAB>error``
(1-based song) for each refused song.
"""

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pysidwizard import (
    SIDFormatError,
    SWMPlayer,
    parse_sid,
)  # noqa: E402  # pylint: disable=wrong-import-position

CALLS = 60


def refusals(args):
    """``(path, song, error)`` for each song of HVSC ``path`` whose first call raises."""
    root, path = args
    try:
        swm = parse_sid((Path(root) / path).read_bytes())
    except (OSError, SIDFormatError):
        return []
    if swm.sid_amount > 1:
        return [(path, 1, "multi-SID")]
    out = []
    for n in range(swm.subtune_count):
        try:
            player = SWMPlayer(swm.subtune(n))
            for _ in range(CALLS):
                player.play_frame()
        except (ValueError, SIDFormatError) as exc:
            out.append((path, n + 1, f"{type(exc).__name__}: {exc}"))
    return out


def main(argv=None) -> None:
    """Parse ``argv`` and run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path)
    parser.add_argument("--hvsc", required=True)
    parser.add_argument("--jobs", type=int, default=None)
    args = parser.parse_args(argv)
    paths = [p for p in args.paths.read_text(encoding="utf-8").split() if p]
    with ProcessPoolExecutor(args.jobs) as pool:
        work = pool.map(refusals, [(args.hvsc, p) for p in paths], chunksize=4)
        for found in tqdm(work, total=len(paths), desc="songs", unit="tune"):
            for path, song, error in found:
                print(f"{path}\t{song}\t{error}", flush=True)


if __name__ == "__main__":
    main()
