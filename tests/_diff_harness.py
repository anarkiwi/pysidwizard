"""Test-only: diff an ``SWMPlayer`` run against a per-frame
SID-register reference capture from real SID-Wizard.

A reference CSV (``frame,reg,value`` schema, dedup'd) comes from
``sidwizard-driver``'s ``capture`` command. This module loads it,
shifts past the F1 boot transition, and compares the freshly-emitted
pysidwizard writes against the captured stream — frame by frame,
register by register.

Comparison unit: "the value of register R after frame F" (cumulative
state). A SET at frame 10 stays visible at frame 11 even if neither
stream re-emits it; a missing or extra write surfaces as a
register-state divergence on the first frame where one stream has
acted on it.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from pysidwizard.player import SID_REG_BASE, SWMPlayer, iter_writes
from pysidwizard.reader import read_swm

# --------------------------------------------------------------------------
# Reference / candidate stream loading.
# --------------------------------------------------------------------------


WriteRow = Tuple[int, int, int]  # (frame, reg_offset, value)


# Per-voice register names so divergence reports read like
# "voice 1 FREQ_HI" rather than "$D408".
_REG_NAMES = {
    0x00: "v0 FREQ_LO",
    0x01: "v0 FREQ_HI",
    0x02: "v0 PW_LO",
    0x03: "v0 PW_HI",
    0x04: "v0 CTRL",
    0x05: "v0 AD",
    0x06: "v0 SR",
    0x07: "v1 FREQ_LO",
    0x08: "v1 FREQ_HI",
    0x09: "v1 PW_LO",
    0x0A: "v1 PW_HI",
    0x0B: "v1 CTRL",
    0x0C: "v1 AD",
    0x0D: "v1 SR",
    0x0E: "v2 FREQ_LO",
    0x0F: "v2 FREQ_HI",
    0x10: "v2 PW_LO",
    0x11: "v2 PW_HI",
    0x12: "v2 CTRL",
    0x13: "v2 AD",
    0x14: "v2 SR",
    0x15: "FILT_CUT_LO",
    0x16: "FILT_CUT_HI",
    0x17: "FILT_RES_RT",
    0x18: "MODE_VOL",
}


def reg_name(r: int) -> str:
    return _REG_NAMES.get(r, f"REG_{r:02X}")


def parse_ignore_regs(spec: str) -> List[int]:
    """Parse a comma-separated list of register offsets (decimal or
    ``0x``-prefixed hex) into integers. Empty string -> empty list."""
    if not spec:
        return []
    out: List[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        out.append(int(chunk, 0))
    return out


def load_reference_csv(path: Path) -> List[WriteRow]:
    """Load a ``frame,reg,value`` CSV. Returns rows in file order."""
    rows: List[WriteRow] = []
    with open(path, newline="") as f:
        rdr = csv.reader(f)
        header = next(rdr, None)
        if header != ["frame", "reg", "value"]:
            raise ValueError(
                f"unexpected CSV header in {path}: {header!r}" " (expected ['frame','reg','value'])"
            )
        for row in rdr:
            if not row:
                continue
            frame, reg, value = (int(x) for x in row[:3])
            rows.append((frame, reg, value))
    return rows


def normalize_reference(rows: Sequence[WriteRow]) -> Tuple[int, List[WriteRow]]:
    """Shift the reference so its first row is at frame 0. Returns
    ``(start_frame_offset, rows_zero_based)``."""
    if not rows:
        return 0, []
    offset = rows[0][0]
    return offset, [(f - offset, r, v) for (f, r, v) in rows]


# Minimum number of distinct zero-value register writes within a single
# frame that we treat as the SID-clear signature of SID-Wizard's "F1
# transition". 15 picks up the real captures (which clear 18 registers
# at once) without false-positiving on actual music frames (which never
# zero more than a handful of regs at a time).
_TRANSITION_ZERO_THRESHOLD = 15

# Only scan the first N normalized frames when auto-detecting; the F1
# transition always lands in the first few frames of any usable capture.
_TRANSITION_SCAN_WINDOW = 5

# Per-voice CTRL register offsets ($D404 / $D40B / $D412).
_CTRL_REGS = (0x04, 0x0B, 0x12)

# SID CTRL bit 3 = test bit. SID-Wizard's player only writes a test-bit
# CTRL value during a hard-restart row trigger, so the first frame
# carrying that bit is the song's first row-0 emit — far more reliable
# than the SID-clear heuristic when the menu IRQ keeps writing through
# the F1 transition.
_CTRL_TEST_BIT = 0x08

# Scan a wider window for the HR-test-bit signal: euphoria sits at frame
# 178 because of its slowdown.
_HR_SCAN_WINDOW = 300


def detect_transition_end(rows: Sequence[WriteRow]) -> int:
    """Return the first post-transition frame index in a normalized
    reference, or 0 if no F1 transition is detected.

    Two strategies, in order:

    1. **SID-clear signature.** Find the LAST frame within the first
       ``_TRANSITION_SCAN_WINDOW`` frames that has at least
       ``_TRANSITION_ZERO_THRESHOLD`` distinct zero-value writes
       (SID-Wizard's F1 SID-clear signature). Return that frame + 1.
       This wins when present because it's an unambiguous structural
       marker.

    2. **HR test-bit detection (fallback).** If no SID-clear appears,
       scan the first ``_HR_SCAN_WINDOW`` frames for the earliest
       frame in which any voice writes its CTRL register with bit 3
       (``$08``) set — i.e. the SID-Wizard player's HR test-bit
       pulse. This marks the song's first row-0 trigger and works
       even when the menu IRQ keeps writing through F1 (the case for
       all four current fixtures). The return value is the
       HR-test-bit frame itself: our player's frame 0 also emits the
       HR test-bit (post-the pre-HR fix), so they align directly.

    Returns 0 if neither signal is found.
    """
    if not rows:
        return 0
    # Strategy 1: SID-clear signature.
    zero_distinct: dict = {}
    for f, r, v in rows:
        if f >= _TRANSITION_SCAN_WINDOW:
            break
        if v == 0:
            zero_distinct.setdefault(f, set()).add(r)
    transition_frame = None
    for f in sorted(zero_distinct):
        if len(zero_distinct[f]) >= _TRANSITION_ZERO_THRESHOLD:
            transition_frame = f
    if transition_frame is not None:
        return transition_frame + 1
    # Strategy 2: first CTRL test-bit emit.
    for f, r, v in rows:
        if f >= _HR_SCAN_WINDOW:
            break
        if r in _CTRL_REGS and (v & _CTRL_TEST_BIT):
            return f
    return 0


# --------------------------------------------------------------------------
# Per-frame state reconstruction.
# --------------------------------------------------------------------------


# 25 registers ($D400..$D418).
N_REGS = 25


def reconstruct_state_per_frame(
    rows: Iterable[WriteRow],
    n_frames: int,
    initial_state: Optional[Sequence[int]] = None,
) -> List[List[int]]:
    """Replay a sequence of dedup'd ``(frame, reg, value)`` writes into a
    per-frame snapshot of the full SID register file.

    A write at frame F establishes the value for frames F, F+1, F+2 ...
    until the next write to the same register. Returns a list of length
    ``n_frames``; each entry is a length-25 list of byte values.

    ``initial_state`` defaults to all zeros (matches the post-reset
    SID); pass a length-25 sequence to seed the reconstruction with a
    different starting point — used by :func:`diff_against_reference`
    to make both sides start from the post-F1-transition SID state
    rather than from cold reset.
    """
    if initial_state is None:
        state = [0] * N_REGS
    else:
        if len(initial_state) != N_REGS:
            raise ValueError(f"initial_state must be length {N_REGS}, got {len(initial_state)}")
        state = list(initial_state)
    snapshots: List[List[int]] = []
    rows_iter = iter(sorted(rows, key=lambda r: r[0]))
    pending: Optional[WriteRow] = next(rows_iter, None)
    for f in range(n_frames):
        while pending is not None and pending[0] == f:
            _, r, v = pending
            if 0 <= r < N_REGS:
                state[r] = v & 0xFF
            pending = next(rows_iter, None)
        snapshots.append(list(state))
    return snapshots


# --------------------------------------------------------------------------
# Diff result types.
# --------------------------------------------------------------------------


@dataclass
class Divergence:
    frame: int  # frame index, zero-based against the reference
    reg: int
    ref_value: int
    our_value: int

    def render(self) -> str:
        return (
            f"frame {self.frame:5d}  reg ${SID_REG_BASE + self.reg:04X} "
            f"({reg_name(self.reg)})  ref=${self.ref_value:02X} "
            f" ours=${self.our_value:02X}"
        )


@dataclass
class DiffResult:
    n_frames: int
    start_frame_offset: int
    skipped_transition_frames: int  # ref frames dropped by --reference-skip
    ignore_regs: List[int]
    first_divergence: Optional[Divergence]
    matched_frames: int  # frames where every non-ignored reg matched
    divergent_register_writes: int  # total per-(frame,reg) cells that differed

    @property
    def matched(self) -> bool:
        return self.first_divergence is None

    def render_summary(self) -> str:
        lines = []
        skip_note = (
            f" + {self.skipped_transition_frames} F1-transition frame(s) dropped"
            if self.skipped_transition_frames
            else ""
        )
        lines.append(
            f"compared {self.n_frames} frames "
            f"(reference shifted by -{self.start_frame_offset}{skip_note})"
        )
        if self.ignore_regs:
            ignored = ",".join(f"${r:02X}" for r in self.ignore_regs)
            lines.append(f"ignored registers: {ignored}")
        if self.matched:
            lines.append("MATCH: every frame and register agrees")
        else:
            d = self.first_divergence
            assert d is not None
            lines.append(
                f"first divergence at frame {d.frame} of {self.n_frames} "
                f"({100.0 * d.frame / max(1, self.n_frames):.1f}% in)"
            )
            lines.append("  " + d.render())
            lines.append(
                f"matched {self.matched_frames}/{self.n_frames} frames clean; "
                f"{self.divergent_register_writes} (frame,reg) cells differ"
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Diff core.
# --------------------------------------------------------------------------


def diff_against_reference(
    swm_path: Path,
    reference_rows: Sequence[WriteRow],
    *,
    limit_frames: Optional[int] = None,
    ignore_regs: Sequence[int] = (),
    dedupe: bool = True,
    skip_transition: bool = False,
    skip_frames: Optional[int] = None,
    ghost_path: Optional[Path] = None,
) -> DiffResult:
    """Run the player on ``swm_path`` and diff its register state against
    ``reference_rows`` (already in the reference's native, unshifted
    frame numbering).

    Comparison span is from frame 0 (after shifting) to
    ``min(limit_frames, last_reference_frame + 1)``.

    ``skip_frames=N`` drops the first ``N`` frames of the normalized
    reference (their writes are NOT applied to the reconstructed state)
    and re-shifts the remaining rows so the comparison starts at our
    player's frame 0. ``skip_transition=True`` auto-detects the F1
    transition via :func:`detect_transition_end` and applies the
    resulting skip count. Explicit ``skip_frames`` wins over
    ``skip_transition``.

    ``ghost_path`` points to a ``{tune}.ghost.csv`` produced by
    ``sidwizard-driver``'s ghost dump. When provided, frame 0 of the
    ghost is used to selectively seed non-STRTSND voices' ZP state
    (editor-residue inheritance — see [[pysidwizard-zp-residue]]).
    """
    start_offset, ref = normalize_reference(reference_rows)
    if not ref:
        raise ValueError("reference is empty")

    skipped = 0
    if skip_frames is not None:
        skipped = skip_frames
    elif skip_transition:
        skipped = detect_transition_end(ref)

    # Reconstruct the reference's full state timeline. Writes from the
    # skipped (F1 transition) frames ARE applied — they seed the initial
    # SID register state that BOTH sides start from at the first
    # compared frame. We slice the snapshots from ``skipped`` onwards
    # and use the state at the end of the last skipped frame as the
    # initial state for our own reconstruction, so an untriggered voice
    # whose SID regs were left non-zero by the F1 transition still
    # matches (the SID retains the F1 writes; our player just doesn't
    # touch them).
    ref_last_frame = ref[-1][0]
    ref_state_full = reconstruct_state_per_frame(ref, ref_last_frame + 1)
    if skipped >= len(ref_state_full):
        raise ValueError(f"skipping {skipped} frame(s) left no reference frames to compare")
    if skipped > 0:
        seed_state: Optional[List[int]] = list(ref_state_full[skipped - 1])
    else:
        seed_state = None
    ref_state = ref_state_full[skipped:]
    n_frames = len(ref_state)
    if limit_frames is not None:
        n_frames = min(n_frames, limit_frames)
        ref_state = ref_state[:n_frames]

    swm = read_swm(str(swm_path))
    player = SWMPlayer(swm)
    if ghost_path is not None:
        from tests._ghost_seed import (
            load_ghost_csv,
            load_ghost_selfmod,
            seed_player_for_cell_diff,
        )

        ghost = load_ghost_csv(ghost_path)
        selfmod_frames = load_ghost_selfmod(ghost_path)
        seed_player_for_cell_diff(
            player,
            ghost.get(0) or {},
            seed_sid_state=list(seed_state) if seed_state else None,
            selfmod_frame0=selfmod_frames.get(0),
        )
    our_rows: List[WriteRow] = list(
        iter_writes(
            swm,
            n_frames,
            dedupe=dedupe,
            initial_state=seed_state,
            player=player,
        )
    )
    our_state = reconstruct_state_per_frame(our_rows, n_frames, initial_state=seed_state)

    ignore = set(ignore_regs)
    cell_diffs = 0
    matched_frames = 0
    first_div: Optional[Divergence] = None

    for f in range(n_frames):
        rfrm = ref_state[f]
        ofrm = our_state[f]
        frame_clean = True
        for r in range(N_REGS):
            if r in ignore:
                continue
            if rfrm[r] != ofrm[r]:
                cell_diffs += 1
                frame_clean = False
                if first_div is None:
                    first_div = Divergence(f, r, rfrm[r], ofrm[r])
        if frame_clean:
            matched_frames += 1

    return DiffResult(
        n_frames=n_frames,
        start_frame_offset=start_offset,
        skipped_transition_frames=skipped,
        ignore_regs=list(ignore_regs),
        first_divergence=first_div,
        matched_frames=matched_frames,
        divergent_register_writes=cell_diffs,
    )


# --------------------------------------------------------------------------
# CLI.
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("reference", type=Path, help="ground-truth CSV from sidwizard-driver")
    ap.add_argument("swm", type=Path, help="SWM module that produced the reference")
    ap.add_argument(
        "--limit-frames",
        type=int,
        default=None,
        help="cap the comparison span (frames). Default: full reference length.",
    )
    ap.add_argument(
        "--ignore-regs",
        type=str,
        default="",
        help="comma-separated register offsets to ignore (e.g. '0x15,0x16,0x17,0x18')",
    )
    ap.add_argument(
        "--first-diff-only",
        action="store_true",
        help="exit non-zero on first divergence without summary stats",
    )
    skip_group = ap.add_mutually_exclusive_group()
    skip_group.add_argument(
        "--skip-transition",
        action="store_true",
        help="auto-detect and skip the F1 keypress transition frames at "
        "the start of the reference (SID-clear signature)",
    )
    skip_group.add_argument(
        "--reference-skip-frames",
        type=int,
        default=None,
        help="explicit count of leading reference frames to drop before "
        "aligning to our player frame 0",
    )
    args = ap.parse_args(argv)

    ref = load_reference_csv(args.reference)
    result = diff_against_reference(
        args.swm,
        ref,
        limit_frames=args.limit_frames,
        ignore_regs=parse_ignore_regs(args.ignore_regs),
        skip_transition=args.skip_transition,
        skip_frames=args.reference_skip_frames,
    )
    if args.first_diff_only:
        if result.matched:
            print("MATCH")
            return 0
        assert result.first_divergence is not None
        print(result.first_divergence.render())
        return 1
    print(result.render_summary())
    return 0 if result.matched else 1


if __name__ == "__main__":
    sys.exit(main())
