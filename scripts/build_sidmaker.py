"""Assemble each release's single-SID SID-Maker and ship it as package data.

Usage: ``python scripts/build_sidmaker.py [VERSION ...]``. Each build is proven to be the
release's own binary, then written to ``src/pysidwizard/data/sidmaker-VERSION.prg`` with
its 64tass labels in ``sidmaker.json``. Needs 64tass, cc, ld and py7zr.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, NamedTuple, Tuple

import py7zr
import pydexomizer
from pysidtracker.testing import default_cache, fetch_verified
from tqdm import tqdm

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests._swm_cache import (  # noqa: E402  # pylint: disable=wrong-import-position
    SW17,
    SW18,
    SW194,
    SW197,
)

DATA = REPO / "src" / "pysidwizard" / "data"
LABELS = (
    "MUSICDATA",
    "PLAYERADDR",
    "SIDHEADER",
    "SIDHEADERSIZE",
    "SIDflags",
    "ModelFlag",
    "DRIVERTYPE_POS",
    "NumberOfPlayerTypes",
    "compzptr",
    "expoendadd",
    "outputformat",
    "relocaddr",
    "PALNTSC",
    "setEXEdetails",
    "preparetunedata",
    "disPtyp",
    "setplayer",
    "SetSIDply",
    "SetSIDt",
)
_OLD = ("-C", "-a")
_NEW = _OLD + (
    "-Wno-deprecated",
    "-DEMULATOR=0",
    "-DTUI_ONLY=0",
    "-DOSCILLOSCOPE_SHOWS_ALL_CHANNELS=0",
)
_SM = ("-D", "SID_AMOUNT=1", "-D", "SFX_SUPPORT=0", "-D", "SLOWDOWN_SUPP=0", "-D", "SWP_EXPORT=0")
_ENC = re.compile(rb"(\.enc +)(screen|none)\b")
_LABEL = re.compile(r"^(\w+)\s*=\s*(\$[0-9a-fA-F]+|\d+)\s*$")


class Release(NamedTuple):
    """Where a release keeps its SID-Maker sources and binary, and how it packed it."""

    archive: Tuple[str, str]
    root: str
    aflags: Tuple[str, ...]
    appversion: str
    binary: str
    packer: str


RELEASES: Dict[str, Release] = {
    "1.7": Release(
        SW17, "SID-Wizard-1.7-smallpack", _OLD, "1.7", "application/SID-Maker-1.7.prg", "exomizer"
    ),
    "1.8": Release(
        SW18, "SID-Wizard-1.8", _OLD, "1.8", "application/SID-Maker-1.8.prg", "exomizer"
    ),
    "1.94": Release(
        SW194, "SID-Wizard-1.94/native", _NEW, "1.9", "application/SID-Maker-1.9.prg", "exomizer"
    ),
    "1.97": Release(
        SW197,
        "SID-Wizard-1.97-sources-examples/native",
        _NEW,
        "1.9",
        "application/SID-Maker-1.9.prg",
        "compress64k",
    ),
}


def extract(archive: Path, dest: Path) -> None:
    """Unpack a tar, zip or 7z ``archive`` into ``dest``."""
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    elif py7zr.is_7zfile(archive):
        with py7zr.SevenZipFile(archive) as sz:
            sz.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")


def quote_encodings(sources: Path) -> None:
    """Quote the bare ``.enc screen`` / ``.enc none`` names current 64tass rejects."""
    for path in sources.rglob("*"):
        if path.suffix in (".asm", ".inc", ".cfg"):
            text = path.read_bytes()
            fixed = _ENC.sub(rb'\1"\2"', text)
            if fixed != text:
                path.write_bytes(fixed)


def assemble(release: Release, sources: Path, out: Path) -> Dict[str, int]:
    """Assemble ``exporter.asm`` to ``out``; the label file's integer labels."""
    labels = out.with_suffix(".lbl")
    subprocess.run(
        ["64tass", *release.aflags, *_SM, "-D", f"SWversion='{release.appversion}'"]
        + ["-o", str(out), "-l", str(labels), "exporter.asm"],
        cwd=sources,
        check=True,
        capture_output=True,
    )
    found = {}
    for line in labels.read_text().splitlines():
        match = _LABEL.match(line)
        if match:
            value = match[2]
            found[match[1]] = int(value[1:], 16) if value[0] == "$" else int(value)
    return found


def packed_by_compress64k(prg: bytes, tools: Path, work: Path) -> bytes:
    """``prg`` packed by the release's own ``compress64kSFX``, built from its C sources."""
    build_dir = work / "compress64k"
    build_dir.mkdir()
    blobs = ["selfextract.prg", "selfextract_nomove.prg"]
    for name in ["compress64k.c", "compressor.c", "decompressor.c", "compression.h", *blobs]:
        shutil.copy(tools / name, build_dir)
    subprocess.run(["ld", "-r", "-b", "binary", "-o", "sfx.o", *blobs], cwd=build_dir, check=True)
    subprocess.run(
        ["cc", "-O2", "-w", "-o", "compress64kSFX", "compress64k.c", "sfx.o", "-lm"],
        cwd=build_dir,
        check=True,
    )
    (build_dir / "in.prg").write_bytes(prg)
    subprocess.run(
        ["./compress64kSFX", "in.prg", "out.prg"], cwd=build_dir, check=True, capture_output=True
    )
    return (build_dir / "out.prg").read_bytes()


def verify(release: Release, prg: bytes, root: Path, work: Path) -> None:
    """Raise unless ``prg`` is the release's own SID-Maker binary."""
    shipped = (root / release.binary).read_bytes()
    if release.packer == "exomizer":
        unpacked = pydexomizer.decrunch_sfx(shipped)
        same = (unpacked.start, bytes(unpacked.data)) == (prg[0] | prg[1] << 8, prg[2:])
    else:
        same = packed_by_compress64k(prg, root / "sources" / "tools", work) == shipped
    if not same:
        raise SystemExit(f"assembled SID-Maker differs from {release.binary}")


def build(version: str, work: Path) -> Dict[str, int]:
    """Assemble, verify and write one release's SID-Maker; its shipped labels."""
    release = RELEASES[version]
    archive = fetch_verified(*release.archive, cache_dir=default_cache("archives", REPO))
    extract(archive, work)
    root = work / release.root
    quote_encodings(root / "sources")
    out = work / f"sidmaker-{version}.prg"
    labels = assemble(release, root / "sources", out)
    prg = out.read_bytes()
    verify(release, prg, root, work)
    (DATA / out.name).write_bytes(prg)
    return {name: labels[name] for name in LABELS}


def main(argv=None) -> None:
    """Build the requested releases (default: all) and merge their labels."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("versions", nargs="*", metavar="VERSION", help=", ".join(RELEASES))
    versions = parser.parse_args(argv).versions or list(RELEASES)
    unknown = sorted(set(versions) - set(RELEASES))
    if unknown:
        parser.error(f"unknown release(s): {', '.join(unknown)}")
    DATA.mkdir(parents=True, exist_ok=True)
    table = DATA / "sidmaker.json"
    known = json.loads(table.read_text()) if table.exists() else {}
    with tempfile.TemporaryDirectory() as tmp:
        for version in tqdm(versions, desc="SID-Maker", unit="release"):
            known[version] = build(version, Path(tmp) / version)
    ordered = {v: known[v] for v in sorted(known, key=lambda v: tuple(map(int, v.split("."))))}
    table.write_text(json.dumps(ordered, indent=1) + "\n")


if __name__ == "__main__":
    main()
