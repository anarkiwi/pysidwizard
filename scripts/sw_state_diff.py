"""Play one tune under the transliterated spec and a state-based one, and print the first call
whose player variables differ.

``sw_state_diff.py TUNE OLD NEW [--frames N]``: the old spec's variables live in memory at the binder's
addresses, the new spec's in its state.
"""

import argparse
import dataclasses
import re
import sys
from pathlib import Path

VOICE = (
    "freq pw wfghost ptngate pweepcnt packcnt spdcnt seqpos ptnpos wftpos pwtpos arpscnt curptn"
    " curnot dpitch curifx curins curfx2 curval slidevib freqmod videlcnt vibfrequ vibracnt transp"
    " tmpbase tmppos arpsped pkbdtrk curchord chordpos flswtbl flswtb2 trktmpos seqtempo transp2"
    " detuner seqt"
).split()
ADDR = {"freq": "freqlo", "pw": "pwlogho", "freqmod": "freqmodl"}


def player(path, spec, native=True):
    """A native spec player of ``path`` under the spec file ``spec``."""
    # pylint: disable=import-outside-toplevel
    from pysidtracker import find_format
    from pysidtracker.playspec import load

    raw = Path(path).read_bytes()
    fmt = find_format(raw)
    alt = load(spec)
    use = dataclasses.replace(fmt, spec=lambda m: dataclasses.replace(fmt.spec(m), spec=alt))
    return use.spec_player(fmt.parser.parse(raw), native=native)


def main() -> int:
    """Print the first differing variables."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("tune")
    parser.add_argument("old")
    parser.add_argument("new")
    parser.add_argument("--frames", type=int, default=40)
    args = parser.parse_args()
    old, new = player(args.tune, args.old), player(args.tune, args.new)
    for call in range(args.frames):
        old.play_frame()
        new.play_frame()
        layout = old._bind(old.view(), old.entry)  # pylint: disable=protected-access
        mem = old.memory
        bad = []
        for entry, (
            program,
            _f,
            state,
        ) in new._compiled.items():  # pylint: disable=protected-access
            if entry[1] != new.entry:
                continue
            names = [re.sub(r"_\d+$", "", str(c[0])[6:]) for c in program.controls]
            seen = {}
            for name, val in zip(names, state, strict=True):
                seen[name] = seen.get(name, -1) + 1
                if name in VOICE:
                    voice = seen[name]
                    cands = []
                    for v in range(3):
                        addr = layout[ADDR.get(name, name) if name != "seqt" else "p_seqt1"]
                        addr += 4 * v if name == "seqt" else 7 * v
                        width = 2 if name in ("freq", "pw", "freqmod", "seqt") else 1
                        cands.append(int.from_bytes(mem[addr : addr + width], "little"))
                    if val not in cands:
                        bad.append((name, voice, cands, val))
                elif name in layout:
                    if mem[layout[name]] != val:
                        bad.append((name, mem[layout[name]], val))
        if bad:
            print(call, bad)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
