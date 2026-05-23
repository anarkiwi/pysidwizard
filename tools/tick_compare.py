"""Tick-by-tick lockstep comparator: run pysidwizard side-by-side with
the SID-Wizard ghost-dump trace and report the FIRST tick where any
tracked ZP variable diverges.

Pre-requisite: ``/tmp/<tune>_ghost.csv`` from
``python -m sidwizard_driver.ghost_dump``.

The ghost dump halts at ``$1003`` (PLAYER entry) and dumps ZP $10-$80
on each halt. For frame_speed=2 (multispeed) tunes, that means each
ghost-frame K = state at the K-th PLAYER tick entry; MULPLY ticks
between them are NOT captured by ghost_dump but our model still has
to run them between consecutive PLAYER ticks.

We auto-align by finding the first ghost-frame where the v0 SPDCNT
byte equals $01 (= the first captured TICK_0 of a row). Our model's
iter 0 is also a TICK_0, so we lock those together.

CLI: ``python tools/tick_compare.py --tune euphoria [--max-ticks 60]``
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pysidwizard.player import SWMPlayer  # noqa: E402
from pysidwizard.reader import read_swm  # noqa: E402

# Tracked ZP addresses -> (label, getter taking player.voices[vi]).
# Per-voice stride 7; we track all 3 voices for the per-voice variables.
PER_VOICE_TRACKED = {
    0x10: ("FREQLO", lambda v: v.sid_freq_lo),
    0x11: ("FREQHI", lambda v: v.sid_freq_hi),
    0x12: ("PWLOGHO", lambda v: v.sid_pw_lo),
    0x13: ("PWHIGHO", lambda v: v.sid_pw_hi),
    0x14: ("WFGHOST", lambda v: v.sid_ctrl),
    0x15: ("PTNGATE", lambda v: v.ptn_gate),
    0x26: ("SPDCNT", lambda v: v.speed_counter),
    0x29: ("WFTPOS", lambda v: _our_wftpos(v)),
    0x2A: ("PWTPOS", lambda v: _our_pwtpos(v)),
    0x2B: ("ARPSCNT", lambda v: v.arp_speed_counter & 0xFF),
    0x3B: ("CURNOT", lambda v: v.note & 0x7F),
    0x3C: ("DPITCH", lambda v: (v.note + v.octave_shift + v.transpose) & 0x7F),
    0x3D: ("CURIFX", lambda v: 0),  # we don't track CURIFX directly
    0x3E: ("CURINS", lambda v: v.instrument_idx if v.instrument_idx else 0),
    0x4F: ("SLIDEVIB", lambda v: v.slide_vib & 0xFF),
    0x50: ("FREQMODL", lambda v: v.vibrato_freqmod_lo),
    0x51: ("FREQMODH", lambda v: v.vibrato_freqmod_hi),
}

PER_VOICE_STRIDE = 7
INST_WF_TABLE_POS = 0x10


def _our_wftpos(v) -> int:
    if v.instrument is None:
        return 0
    return (INST_WF_TABLE_POS + v.wf_pos) & 0xFF


def _our_pwtpos(v) -> int:
    if v.instrument is None:
        return 0
    return (INST_WF_TABLE_POS + len(v.instrument.wf_table) + 1 + v.pw_pos) & 0xFF


def load_ghost(path: Path) -> Dict[int, Dict[int, int]]:
    out: Dict[int, Dict[int, int]] = {}
    with open(path, newline="") as fp:
        r = csv.DictReader(fp)
        for row in r:
            f = int(row["frame"])
            a = int(row["addr"])
            v = int(row["value"])
            out.setdefault(f, {})[a] = v
    return out


def find_first_row_trigger(ghost: Dict[int, Dict[int, int]]) -> int:
    """Return the first ghost frame where v0 SPDCNT (= $26) is $01.
    That's the first captured TICK_0 of a row for voice 0 — the
    natural alignment point against our model's iter 0."""
    for f in sorted(ghost):
        if ghost[f].get(0x26) == 0x01:
            return f
    raise RuntimeError("no row trigger found in ghost trace")


def compare_tick(
    player: SWMPlayer,
    ref: Dict[int, int],
) -> List[str]:
    diffs: List[str] = []
    for base, (name, getter) in PER_VOICE_TRACKED.items():
        for vi in range(3):
            addr = base + vi * PER_VOICE_STRIDE
            if addr not in ref:
                continue
            ref_val = ref[addr]
            try:
                our_val = getter(player.voices[vi]) & 0xFF
            except Exception as e:  # noqa: BLE001
                diffs.append(f"  {name}_v{vi} @${addr:02X}: getter error {e}")
                continue
            if our_val != ref_val:
                diffs.append(f"  ${addr:02X} {name}_v{vi}: ref=${ref_val:02X} ours=${our_val:02X}")
    return diffs


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tune", default="euphoria")
    ap.add_argument("--max-ticks", type=int, default=60)
    ap.add_argument(
        "--ghost",
        type=Path,
        help="path to ghost CSV (default: /tmp/<tune>_ghost.csv)",
    )
    ap.add_argument(
        "--align-offset",
        type=int,
        default=None,
        help="manual ghost-frame offset (default: auto-detect first SPDCNT=$01)",
    )
    ap.add_argument(
        "--report-all",
        action="store_true",
        help="don't stop at first divergence; show all of them",
    )
    args = ap.parse_args(argv)

    # Fetch the tune from the SID-Wizard tarball cache; SWM bytes are
    # not tracked in-repo (see tests/_swm_cache.py).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tests._swm_cache import swm_path as _fetch_swm

    swm_path = _fetch_swm(args.tune)
    ghost_path = args.ghost or Path(f"/tmp/{args.tune}_ghost.csv")
    if not ghost_path.exists():
        print(f"ghost file not found: {ghost_path}", file=sys.stderr)
        return 2

    ghost = load_ghost(ghost_path)
    n_ghost = max(ghost) + 1
    if args.align_offset is not None:
        align = args.align_offset
    else:
        align = find_first_row_trigger(ghost)
    print(f"aligning ghost frame {align} = our PLAYER tick 0")

    swm = read_swm(str(swm_path))
    print(f"frame_speed = {swm.frame_speed}")
    player = SWMPlayer(swm)
    frame_step = max(1, swm.frame_speed)

    first_div_tick: Optional[int] = None
    n_compared = 0
    for tick in range(args.max_ticks):
        ghost_frame = align + tick
        if ghost_frame >= n_ghost:
            break
        ref = ghost[ghost_frame]
        diffs = compare_tick(player, ref)
        if diffs:
            print(f"\ntick {tick:>3} (ghost {ghost_frame:>3}):")
            for d in diffs:
                print(d)
            if first_div_tick is None:
                first_div_tick = tick
            if not args.report_all:
                return 1
        n_compared += 1
        # Advance our model one PLAYER tick worth.
        for _ in range(frame_step):
            player.play_frame()

    if first_div_tick is None:
        print(f"\nCLEAN MATCH over {n_compared} ticks")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
