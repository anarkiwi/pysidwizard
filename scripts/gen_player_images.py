"""Generate ``pysidwizard.player_images`` from a release's ``.sid`` exports.

A bare ``.swm`` has no player image, so past-``FREQTBL`` lookups would read a
fallback; the real per-(version, driver) bytes come from the release's own
``.sid`` exports, asserted byte-identical across every tune of a release+driver.
"""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

REPO = Path(__file__).resolve().parent.parent
CACHE = REPO / "tests" / ".swmcache"
DEFAULT_OUT = REPO / "src" / "pysidwizard" / "player_images.py"

# version -> (archive name, kind, glob of 1-SID exports); the 1.7 smallpack is the pre-1.8 release
SOURCES: List[Dict] = [
    {
        "version": "1.97",
        "archive": "SID-Wizard-1.97-sources-examples.tar.gz",
        "kind": "tar",
        "globs": ["native/examples/sid-exports/*.sid", "resources/examples/new-sid-exports/*.sid"],
    },
    {
        "version": "1.7",
        "archive": "SID-Wizard-1.7-smallpack.zip",
        "kind": "zip",
        "globs": ["SID-Wizard-1.7-smallpack/demotunes/sid-exports/*.sid"],
    },
]

_DOC = (
    '"""FREQTBL / FREQTBH images for bare-``.swm`` playback, per (version, driver).\n'
    "\n"
    "A bare ``.swm`` carries no player image, so the player's lookups past the 96-note\n"
    "``FREQTBL`` would otherwise read a constant fallback. Each release+driver build sits\n"
    "different bytes over that region, and the pre-1.8 first-frame quirk is per release.\n"
    "This ships the exact 256-byte spans and the first-frame flag for each pair, taken\n"
    "from that release's own ``.sid`` exports (its player at ``PLAYERADDR``) and asserted\n"
    "byte-identical across every tune of a release+driver.\n"
    "\n"
    "Regenerate with ``python scripts/gen_player_images.py``; only this module is committed.\n"
    '"""'
    "\n"
)


@dataclass
class _Image:
    lo: bytes  # 256-byte span from FREQTBL base
    hi: bytes  # 256-byte span from FREQTBH base
    first_frame: bool  # True = pre-1.8 FREQTBH-by-track quirk
    sources: List[str] = field(default_factory=list)


def _match(glob: str, name: str) -> bool:
    pattern = re.escape(glob).replace(re.escape("*"), "[^/]*")  # * never spans a /
    return re.search(pattern, name.replace("\\", "/")) is not None


def _iter_sids(archive: Path, kind: str, globs: List[str]) -> List[Tuple[bytes, str]]:
    out: List[Tuple[bytes, str]] = []
    if kind == "tar":
        with tarfile.open(archive, "r:gz") as tf:
            for name in tf.getnames():
                if any(_match(g, name) for g in globs):
                    out.append((tf.extractfile(name).read(), name))
    else:
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if any(_match(g, name) for g in globs):
                    out.append((zf.read(name), name))
    return out


def collect() -> Dict[Tuple[str, int], _Image]:
    """Map each (version, driver) in the cached exports to its player image.

    Raises if the exports for a release+driver disagree, which would mean the
    past-table bytes are not a function of (version, driver).
    """
    from pysidwizard.sidreader import parse_sid

    groups: Dict[Tuple[str, int], _Image] = {}
    failures: List[str] = []
    for src in SOURCES:
        archive = CACHE / src["archive"]
        if not archive.is_file():
            failures.append(f"missing archive {archive.name}")
            continue
        for data, srcname in _iter_sids(archive, src["kind"], src["globs"]):
            try:
                s = parse_sid(data)
            except Exception:
                continue  # not a single-SID export with a FREQTBL load
            if not (s.freq_lo_image and s.freq_hi_image):
                continue
            key = (src["version"], s.driver_type)
            img = groups.get(key)
            if img is None:
                groups[key] = _Image(
                    s.freq_lo_image, s.freq_hi_image, s.first_frame_freq_by_track, [srcname]
                )
            elif (
                img.lo != s.freq_lo_image
                or img.hi != s.freq_hi_image
                or img.first_frame != s.first_frame_freq_by_track
            ):
                failures.append(f"{key} disagrees with {srcname}")
            else:
                img.sources.append(srcname)
    if failures:
        raise SystemExit("image consistency failures:\n" + "\n".join(failures))
    return groups


def render(groups: Dict[Tuple[str, int], _Image]) -> str:
    """Emit the ``player_images`` module source for ``groups``."""
    versions = sorted({v for (v, _) in groups})
    latest = max(versions)
    body: List[str] = [
        "from __future__ import annotations",
        "",
        "# Releases with a shipped player image.",
        f"PLAYER_VERSIONS = ({', '.join(repr(v) for v in versions)})",
        "",
        "# Latest release; the default for player_version.",
        f"LATEST_PLAYER_VERSION = {latest!r}",
        "",
        "# (version, driver_type) -> (freq_lo_image, freq_hi_image, first_frame_freq_by_track);",
        "# the 256-byte FREQTBL / FREQTBH spans as the 6502 pitch table for that build.",
        "_PLAYER_IMAGES = {",
    ]
    for ver in versions:
        for d in sorted(d for (v, d) in groups if v == ver):
            img = groups[(ver, d)]
            body.append(f'    ({ver!r}, {d}): (bytes.fromhex("{img.lo.hex()}"),')
            body.append(f'        bytes.fromhex("{img.hi.hex()}"), {img.first_frame}),')
    body += [
        "}",
        "",
        "def image_for(version: str, driver_type: int) -> tuple[bytes, bytes, bool] | None:",
        '    """The image for ``(version, driver_type)`` or None if that pair is not shipped."""',
        "    return _PLAYER_IMAGES.get((version, driver_type))",
        "",
    ]
    return _DOC + "\n" + "\n".join(body)


def main(out: Path) -> None:
    groups = collect()
    for (ver, drv), img in sorted(groups.items()):
        name = next((Path(s).name for s in img.sources), "?")
        print(
            f"{ver:>4} driver {drv}: first_frame={img.first_frame}  {len(img.sources)} agree  e.g. {name}"
        )
    import black

    source = black.format_file_contents(render(groups), fast=False, mode=black.Mode())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(source)
    print(f"wrote {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT)
