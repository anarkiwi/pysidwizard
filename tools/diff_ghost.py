"""Diff a SID-Wizard ghost-register dump (from
``sidwizard_driver.ghost_dump``) against pysidwizard's internal player
state on the same SWM module.

The ghost-dump CSV gives the real player's zero-page state ($10..$80)
at the start of each PAL frame. This tool runs ``SWMPlayer`` for the
same number of frames and reports the FIRST frame where any
SID-Wizard player variable that we model diverges from the ref —
without going through the SID-write reconstruction that conflates
every feature.

Use this as the inner-loop signal when implementing individual player
features (vibrato, arp-speed, chord pitch, etc.). The "what changed
internally" answer is much sharper than "which cells differ on the SID".

CLI: ``python tools/diff_ghost.py
        --ghost /tmp/flashitback_ghost.csv
        --swm tests/data/flashitback.swm
        [--limit-frames 60]``
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any, Callable, Optional

# Make pysidwizard importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pysidwizard.player import SWMPlayer  # noqa: E402
from pysidwizard.reader import read_swm  # noqa: E402

# Per-voice stride is 7 (player.asm constants table @ ~line 250).
PER_VOICE_STRIDE = 7

# Map of (zp_address -> ("var_name", getter)) where ``getter(player, vi)``
# returns the value our SWMPlayer thinks the variable should hold for
# voice ``vi``. Used to compute a per-row "our_value" alongside ref's
# byte at that address. ``None`` means "we don't track this yet" and
# the row is dropped from the diff.
# INST_WF_TABLE_POS = $10 — the WF table starts at byte $10 of each
# instrument's data block. SID-Wizard's WFTPOS is an absolute byte
# offset into the instrument data (so $10 = "at start of WF table");
# our wf_pos is 0-relative to wf_table[], so the equivalence is
# WFTPOS = wf_pos + $10. PWTPOS / FILTPOS apply similar per-instrument
# offsets based on the cumulative table sizes.
_INST_WF_TABLE_POS = 0x10


def _our_pwtpos(v: Any) -> int:
    """``pw_pos`` is already SID-Wizard's instrument-base byte offset
    (absolute PWTPOS); just mask it to a byte."""
    if v.instrument is None:
        return 0
    return v.pw_pos & 0xFF


_PER_VOICE: dict[int, tuple[str, Callable[[Any], int]]] = {
    # Block 1 ($10-$16): SID ghost registers (per voice, 7-byte stride).
    0x10: ("FREQLO", lambda v: v.sid_freq_lo),
    0x11: ("FREQHI", lambda v: v.sid_freq_hi),
    0x12: ("PWLOGHO", lambda v: v.sid_pw_lo),
    0x13: ("PWHIGHO", lambda v: v.sid_pw_hi),
    0x14: ("WFGHOST", lambda v: v.sid_ctrl),
    0x15: ("PTNGATE", lambda v: v.ptn_gate),
    # PWEEPCNT (= our pw_sweep_count) — only roughly aligned; skip.
    # Block 2 ($25-$2B): sequence/pattern/table position state. Determined
    # empirically by watching which bytes evolve as expected during
    # row-trigger / WF-walk cycles (see ghost-register-bench memo).
    0x26: ("SPDCNT", lambda v: v.speed_counter),
    # WFTPOS / PWTPOS are absolute byte offsets into the instrument
    # data block. Convert our 0-relative table indices into the same
    # frame of reference for a direct compare.
    0x29: ("WFTPOS", lambda v: (v.wf_pos + _INST_WF_TABLE_POS) & 0xFF),
    0x2A: ("PWTPOS", _our_pwtpos),
    # Block 3 ($3A-$40): note / instrument / fx columns.
    # SID-Wizard's DPITCH = note + octave_shift + transpose (the
    # computed pitch). Our ``v.note`` only holds the raw row note;
    # combine to compare apples to apples. The clamp matches player.asm
    # which masks to 0..127 via ``and #$7F`` before lookup.
    0x3C: ("DPITCH", lambda v: (v.note + v.octave_shift + v.transpose) & 0x7F),
}


def load_ghost(path: Path) -> dict[int, dict[int, int]]:
    """Return ``{frame: {addr: value}}`` from a ghost-dump CSV."""
    out: dict[int, dict[int, int]] = {}
    with open(path, newline="") as fp:
        r = csv.DictReader(fp)
        for row in r:
            f = int(row["frame"])
            a = int(row["addr"])
            v = int(row["value"])
            out.setdefault(f, {})[a] = v
    return out


def diff(
    swm_path: Path,
    ghost: dict[int, dict[int, int]],
    limit_frames: Optional[int] = None,
    ghost_offset: int = 0,
) -> int:
    """Run our player frame-by-frame and report the first divergence
    per per-voice variable. Returns 0 if nothing diverged, 1 otherwise.

    ``ghost_offset`` shifts the ghost dump backward by N frames before
    comparing — useful when the dump's "frame 0" is actually N frames
    after the song's true row-0 trigger (the bench halts at the first
    ``$1003`` hit after F1, which is some frames after the IRQ chain
    has primed itself). Pick a value so that the first vibrato-active
    frame in the dump corresponds to our player's first vibrato frame.
    """
    swm = read_swm(str(swm_path))
    player = SWMPlayer(swm)
    n = max(ghost) + 1 if ghost else 0
    if limit_frames is not None:
        n = min(n, limit_frames)
    # The bench halts at $1003 (PLAYER entry) which fires once per
    # video frame regardless of frame_speed. For frame_speed=N tunes
    # we need to call play_frame N times per ghost frame so that the
    # N-1 MULPLY ticks plus the 1 PLAYER tick run between captures.
    play_frames_per_ghost = max(1, swm.frame_speed)
    first_div: list[str] = []
    first_div_seen: set[tuple[str, int]] = set()
    for frame in range(n):
        for _ in range(play_frames_per_ghost):
            player.play_frame()
        ref_idx = frame + ghost_offset
        ref = ghost.get(ref_idx, {})
        if not ref:
            continue
        for base, (name, getter) in _PER_VOICE.items():
            for vi in range(3):
                addr = base + vi * PER_VOICE_STRIDE
                if addr not in ref:
                    continue
                ref_val = ref[addr]
                try:
                    our_val = getter(player.voices[vi]) & 0xFF
                except Exception as e:  # noqa: BLE001
                    our_val = f"<error: {e}>"
                key = (name, vi)
                if our_val != ref_val and key not in first_div_seen:
                    first_div_seen.add(key)
                    first_div.append(
                        f"frame {frame:4d} (ghost {ref_idx})  "
                        f"${addr:02X} {name}_v{vi}  "
                        f"ref=${ref_val:02X} ours="
                        + (f"${our_val:02X}" if isinstance(our_val, int) else str(our_val))
                    )
    if not first_div:
        print(f"MATCH: tracked {len(_PER_VOICE) * 3} per-voice vars over {n} frames")
        return 0
    print(
        f"first-divergence per tracked variable (over {n} frames, " f"ghost_offset={ghost_offset}):"
    )
    for line in first_div:
        print(f"  {line}")
    return 1


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ghost", type=Path, required=True)
    ap.add_argument("--swm", type=Path, required=True)
    ap.add_argument("--limit-frames", type=int, default=None)
    ap.add_argument(
        "--ghost-offset",
        type=int,
        default=1,
        help="shift ghost frame indices by this much when comparing. "
        "Default 1 matches the bench's pre-armed-checkpoint alignment: "
        "ghost frame 0 is pre-song-start state, ghost frame 1 is the "
        "HR test bit of song row 0 = our player frame 0 post-play_frame.",
    )
    args = ap.parse_args(argv)
    ghost = load_ghost(args.ghost)
    return diff(args.swm, ghost, args.limit_frames, args.ghost_offset)


if __name__ == "__main__":
    raise SystemExit(main())
