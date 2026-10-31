"""Bind the SID-Wizard playspec to tunes and report each failure.

    python scripts/sw_bind_census.py [--relocations] [TUNE.sid ...]

Without arguments: the 1.97 example exports and the oracle tunes. ``--relocations``
lists every reference that disagrees with the player's address instead of the first.
"""

import argparse
import sys
from pathlib import Path

from pysidtracker import SidImage
from pysidtracker.disasm import image_after_init
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pysidwizard import (  # pylint: disable=wrong-import-position
    parse_sid_song,
    playspec,
)  # noqa: E402  # pylint: disable=wrong-import-position


def default_tunes():
    """The 1.97 example exports and the oracle tunes, by name."""
    # pylint: disable=import-outside-toplevel
    from pysidtracker.testing import resolve_tune

    from tests._swm_cache import SW197_EXPORT_SHA256, sw197_export_path
    from tests.test_oracle import TUNES

    out = {}
    for member in SW197_EXPORT_SHA256:
        if member.startswith("native/examples/sid-exports/"):
            out[Path(member).stem] = sw197_export_path(member)
    for name, path in TUNES.items():
        out[name] = Path(resolve_tune(path))
    return out


def bind(path: Path) -> dict:
    """The binding of the tune's first PLAYER after init, for its parsed variant."""
    data = path.read_bytes()
    view = bytes(image_after_init(SidImage.from_sid(data)).window(0, 0x10000))
    player = parse_sid_song(data, 1).player
    return playspec.binding(view, None, (player.features, player.quirks))


def record_relocations() -> list:
    """Make ``_Scan.put`` keep the first address of a name and log each disagreement."""
    conflicts: list = []

    def put(self, routine, name, value):
        first = self.found.setdefault(name, value)
        if first != value:
            conflicts.append((routine, name, value, first))

    playspec._Scan.put = put  # pylint: disable=protected-access
    return conflicts


def main(argv=None) -> None:
    """Parse ``argv`` and run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tunes", nargs="*", type=Path)
    parser.add_argument("--relocations", action="store_true")
    args = parser.parse_args(argv)
    tunes = {p.stem: p for p in args.tunes} or default_tunes()
    conflicts = record_relocations() if args.relocations else []
    failed = 0
    for name, path in tqdm(tunes.items(), desc="bind", unit="tune"):
        del conflicts[:]
        try:
            bind(path)
        except Exception as exc:  # pylint: disable=broad-except
            failed += 1
            tqdm.write(f"{name}: {exc}")
        for routine, cell, value, first in conflicts:
            tqdm.write(f"{name}: {routine} {cell} ${value:04x} player ${first:04x}")
    print(f"{len(tunes) - failed}/{len(tunes)} bound")


if __name__ == "__main__":
    main()
