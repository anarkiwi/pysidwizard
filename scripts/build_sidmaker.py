"""Assemble each release's single-SID SID-Maker and ship it as package data.

Usage: ``python scripts/build_sidmaker.py [BUILD ...]``. Each build is proven to be the
release's own binary, then written to ``src/pysidwizard/data/sidmaker-BUILD.prg`` with
its 64tass labels in ``sidmaker.json``. Needs 64tass and py7zr.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, NamedTuple, Tuple

import py7zr
import pydexomizer
from pysidtracker.editor import RAM_SIZE
from pysidtracker.emu import run_to_rts_until
from pysidtracker.kernalhost import sys_addr
from pysidtracker.testing import default_cache, fetch_verified
from tqdm import tqdm
from vagrant_moses import memmap as mm
from vagrant_moses.cpu import flat_mpu

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests._swm_cache import (  # noqa: E402  # pylint: disable=wrong-import-position
    SW17,
    SW18,
    SW194,
    SW196,
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
    "EXEstarter",
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
    "setsaveparam",
    "SaveExp",
)
OPTIONAL_LABELS = ("ECHO_SUPPORT", "BASIC_STARTADDRESS", "setEXEdetails2")
_OLD = ("-C", "-a")
_NEW = _OLD + (
    "-Wno-deprecated",
    "-DEMULATOR=0",
    "-DTUI_ONLY=0",
    "-DOSCILLOSCOPE_SHOWS_ALL_CHANNELS=0",
)
_ECHO = ("-D", "ECHO_SUPPORT=1", "-D", "ECHO_DIVISOR=4.0", "-D", "ECHO_MILLISECONDS=400")
_SM = ("-D", "SID_AMOUNT=1", "-D", "SFX_SUPPORT=0", "-D", "SLOWDOWN_SUPP=0", "-D", "SWP_EXPORT=0")
_ENC = re.compile(rb"(\.enc +)(screen|none)\b")
_LABEL = re.compile(r"^(\w+)\s*=\s*(\$[0-9a-fA-F]+|\d+)\s*$")
_MAX_CYCLES = 1_000_000_000


class Release(NamedTuple):
    """Where a release keeps its SID-Maker sources and binary, and how it built and packed it."""

    archive: Tuple[str, str]
    root: str
    aflags: Tuple[str, ...]
    appversion: str
    binary: str
    packer: str


_SW196_ROOT = "SID-Wizard-1.96-sources-examples/native"
_SW197_ROOT = "SID-Wizard-1.97-sources-examples/native"
_MAKER19 = "application/SID-Maker-1.9.prg"
_MAKER_ECHO = "application/SID-Maker-echo.prg"
RELEASES: Dict[str, Release] = {
    "1.7": Release(
        SW17, "SID-Wizard-1.7-smallpack", _OLD, "1.7", "application/SID-Maker-1.7.prg", "exomizer"
    ),
    "1.8": Release(
        SW18, "SID-Wizard-1.8", _OLD, "1.8", "application/SID-Maker-1.8.prg", "exomizer"
    ),
    "1.94": Release(SW194, "SID-Wizard-1.94/native", _NEW, "1.9", _MAKER19, "exomizer"),
    "1.96-echo": Release(SW196, _SW196_ROOT, _NEW + _ECHO, "1.9", _MAKER_ECHO, "compress64k"),
    "1.97": Release(SW197, _SW197_ROOT, _NEW, "1.9", _MAKER19, "compress64k"),
    "1.97-echo": Release(SW197, _SW197_ROOT, _NEW + _ECHO, "1.9", _MAKER_ECHO, "compress64k"),
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


def self_extracted(packed: bytes, start: int) -> bytearray:
    """RAM after ``packed``'s own self-extractor runs from its ``SYS`` until it reaches ``start``.

    The extractor leaves the loaded PRG for its relocated decruncher, which ends at ``start``.
    """
    ram = bytearray(RAM_SIZE)
    ram[mm.CPU_DDR], ram[mm.CPU_PORT] = mm.DDR_DEFAULT, mm.BANK_DEFAULT
    load = packed[0] | packed[1] << 8
    ram[load : load + len(packed) - 2] = packed[2:]
    mpu, mem = flat_mpu(ram)
    if not run_to_rts_until(mpu, mem, sys_addr(ram), 0, set(range(load)), _MAX_CYCLES):
        raise SystemExit("self-extractor never left its PRG")
    if not run_to_rts_until(mpu, mem, mpu.pc, 0, {start}, _MAX_CYCLES):
        raise SystemExit(f"self-extractor never reached ${start:04X}")
    return ram


def verify(release: Release, prg: bytes, start: int, root: Path) -> None:
    """Raise unless ``prg`` is the release's own SID-Maker binary."""
    shipped = (root / release.binary).read_bytes()
    load, body = prg[0] | prg[1] << 8, prg[2:]
    if release.packer == "exomizer":
        unpacked = pydexomizer.decrunch_sfx(shipped)
        same = (unpacked.start, bytes(unpacked.data)) == (load, body)
    else:
        same = self_extracted(shipped, start)[load : load + len(body)] == body
    if not same:
        raise SystemExit(f"assembled SID-Maker differs from {release.binary}")


def build(name: str, work: Path) -> Dict[str, int]:
    """Assemble, verify and write one SID-Maker build; its shipped labels."""
    release = RELEASES[name]
    archive = fetch_verified(*release.archive, cache_dir=default_cache("archives", REPO))
    extract(archive, work)
    root = work / release.root
    quote_encodings(root / "sources")
    out = work / f"sidmaker-{name}.prg"
    labels = assemble(release, root / "sources", out)
    prg = out.read_bytes()
    verify(release, prg, labels["start"], root)
    (DATA / out.name).write_bytes(prg)
    return {n: labels[n] for n in LABELS + OPTIONAL_LABELS if n in labels}


def _order(name: str):
    version, _, variant = name.partition("-")
    return tuple(map(int, version.split("."))), variant


def main(argv=None) -> None:
    """Build the requested SID-Makers (default: all) and merge their labels."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("builds", nargs="*", metavar="BUILD", help=", ".join(RELEASES))
    names = parser.parse_args(argv).builds or list(RELEASES)
    unknown = sorted(set(names) - set(RELEASES))
    if unknown:
        parser.error(f"unknown build(s): {', '.join(unknown)}")
    DATA.mkdir(parents=True, exist_ok=True)
    table = DATA / "sidmaker.json"
    known = json.loads(table.read_text()) if table.exists() else {}
    with tempfile.TemporaryDirectory() as tmp:
        for name in tqdm(names, desc="SID-Maker", unit="build"):
            known[name] = build(name, Path(tmp) / name)
    table.write_text(json.dumps({n: known[n] for n in sorted(known, key=_order)}, indent=1) + "\n")


if __name__ == "__main__":
    main()
