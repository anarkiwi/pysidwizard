"""Bind the SID-Wizard playspec to tunes and report each failure.

    python scripts/sw_bind_census.py [TUNE.sid ...]

Without arguments: the 1.97 example exports and the oracle tunes.
"""

import argparse
import sys
from pathlib import Path

from pysidtracker import SidImage
from pysidtracker.disasm import image_after_init
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pysidwizard import parse_sid_song, playspec  # noqa: E402


def default_tunes():
    """The 1.97 example exports and the oracle tunes, by name."""
    from pysidtracker.testing import resolve_tune  # pylint: disable=import-outside-toplevel

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


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tunes", nargs="*", type=Path)
    args = parser.parse_args(argv)
    tunes = {p.stem: p for p in args.tunes} or default_tunes()
    failed = 0
    for name, path in tqdm(tunes.items(), desc="bind", unit="tune"):
        try:
            bind(path)
        except Exception as exc:  # pylint: disable=broad-except
            failed += 1
            tqdm.write(f"{name}: {exc}")
    print(f"{len(tunes) - failed}/{len(tunes)} bound")


if __name__ == "__main__":
    main()
