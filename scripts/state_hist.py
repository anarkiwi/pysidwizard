"""Per-voice gate-state history: player ``ptn_gate`` beside the 6502 PTNGATE slot.

PTNGATE[v] is the 6502 zero-page gate mask at $1026 + 7*v (WFGHOST is $1025 + 7*v).
Prints one line per (frame, voice) where either side's value changed, so the
frame in which the python and the 6502 disagree on the mask is visible.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pysidtracker.image import SidImage
from pysidtracker.oracle import boot_flat, run_to_rts

from pysidwizard import SWMPlayer, parse_sid_song

VOICES = 3
VOICE_STRIDE = 0x07
PTNGATE_BASE = 0x1026
WFGHOST_BASE = 0x1025


def python_hist(data, song, nframes):
    player = SWMPlayer(parse_sid_song(data, song))
    hist = []
    for _ in range(nframes):
        player.play_frame()
        hist.append(
            [(v.ptn_gate, v.in_hr_this_frame, v.zp_dummy, v.triggered) for v in player.voices]
        )
    return hist


def cpu_hist(data, nframes, *, subtune=0, max_cycles=8_000_000):
    image = SidImage.from_bytes(bytes(data))
    _subject, mpu, mem, play = boot_flat(image, subtune, max_cycles)
    hist = []
    for _ in range(nframes):
        run_to_rts(mpu, mem, play, 0, max_cycles)
        hist.append(
            (
                bytes(mem[PTNGATE_BASE : PTNGATE_BASE + 0x20]),
                bytes(mem[WFGHOST_BASE : WFGHOST_BASE + 0x20]),
            )
        )
    return hist


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tunes", nargs="+", type=Path)
    ap.add_argument("--frames", type=int, default=300)
    ap.add_argument("--song", type=int, default=1)
    ap.add_argument("--from-frame", type=int, default=0)
    args = ap.parse_args(argv)

    for tune in args.tunes:
        data = tune.read_bytes()
        py = python_hist(data, args.song, args.frames)
        cpu = cpu_hist(data, args.frames, subtune=args.song - 1)
        print(f"== {tune}")
        prev = [("?", "?")] * VOICES
        for frame in range(args.from_frame, args.frames):
            for v in range(VOICES):
                gate = cpu[frame][0][VOICE_STRIDE * v]
                ghost = cpu[frame][1][VOICE_STRIDE * v]
                ptn, inhr, zpd, trig = py[frame][v]
                cur = (f"{ptn:02X}", f"{gate:02X}")
                if cur != prev[v]:
                    tag = "  DIVERGE" if ptn != gate else ""
                    print(
                        f"  f{frame:3d} v{v+1}: ptn_gate=${ptn:02X} PTNGATE=${gate:02X} "
                        f"WFGHOST=${ghost:02X} in_hr={inhr} zp_dummy={zpd} trig={trig}{tag}"
                    )
                prev[v] = cur
    return 0


if __name__ == "__main__":
    sys.exit(main())
