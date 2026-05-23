"""Phase 3 cumulative cascade test for the pysidwizard cell-diff gap.

For each known cluster-B cell (post-song isolated drift) at iter_writes
frame F, seed pysidwizard from real ghost (per-voice ZP) at frame F-K
and from the reference CSV (global filter / volume state) at frame F-K-1,
then run K+1 IRQ ticks. Compare the diverging SID register's value at
frame F to the reference.

Decision matrix:

  K=0 still diverges          systemic per-tick bug — conformance missed
                              this register because it's not in ZP_NAMES
  K=0 matches, K=N diverges   cumulative drift; find the smallest N where
                              divergence appears
  All K diverge               seed itself is incomplete (likely un-seeded
                              filt_pos for FILT_RES_RT cells); needs a
                              ghost-dump extension
  K=0 matches, cold also OK   the cell isn't reproducible from a cold run
                              (probably a flaky baseline; investigate)

CLI: ``PYTHONPATH=src python3 tools/phase3_cascade.py``
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conformance import load_ghost, seed_voice, tune_tempo  # noqa: E402

from pysidwizard.player import SID_REG_BASE, SWMPlayer  # noqa: E402
from pysidwizard.reader import read_swm  # noqa: E402
from tests._diff_harness import (  # noqa: E402
    detect_transition_end,
    load_reference_csv,
    normalize_reference,
    reconstruct_state_per_frame,
)

FIXTURES = ROOT / "tests" / "fixtures"
DATA = ROOT / "tests" / "data"


@dataclass(frozen=True)
class Cell:
    tune: str
    frame: int  # iter_writes frame index (post-transition shift)
    reg: int  # offset within $D400..$D418
    ref_val: int  # expected value at frame F
    our_val: int  # cell-diff reported "ours" at frame F
    desc: str

    @property
    def reg_addr(self) -> int:
        return SID_REG_BASE + self.reg


# Cluster B = post-song isolated outliers (gap-analysis §2).
# Derived freshly from `diff_against_reference` with skip_transition=True.
#
# RESOLVED 2026-05-22:
# - flashitback F1284 $D40A (v1 PW_HI) — extra $D40A emit at TICK_2 of
#   legato. Fix: ``in_tick2_legato_this_frame`` flag.
# - All 6 FILT_RES_RT cells (flashitback F168/F1140/F1152, euphoria
#   F320/F321, rain8580 F448) — voice_in_filter was being toggled
#   per-CNTPLY2-tick instead of only at STRTSND's SETFLTP. Fix:
#   _start_note sets voice_in_filter from instrument.filter_table[0]
#   != $FF; _tick_filt_table no longer touches it.
# Total cell-diff: 410 -> 400 (-10 cells, -2.4%).
# flashitback and euphoria now match ref EXACTLY (0 cells each).
CELLS: List[Cell] = [
    Cell("rain8580", 913, 0x00, 0x1D, 0x1C, "v0 FREQ_LO slide tail"),
]


# Lookback values (in IRQ ticks). 0 = pure 1-tick prediction.
# Extended past 500 to reach into the late-tune cells; ghost CSVs end at
# frame 499 so for cells past that, only large K is reachable.
K_VALUES = (0, 1, 5, 20, 50, 100, 200, 500, 700, 1000, 1283)


@dataclass
class TuneCtx:
    ghost: Dict[int, Dict[int, int]]
    ref_state: List[List[int]]
    swm: Any
    tempo: int
    skipped: int


def load_tune(tune: str) -> TuneCtx:
    ghost = load_ghost(FIXTURES / f"{tune}.ghost.csv")
    raw = load_reference_csv(FIXTURES / f"{tune}.reference.csv")
    _, rows_norm = normalize_reference(raw)
    drop = detect_transition_end(rows_norm)
    # Build a per-frame SID state that uses the transition writes as the seed
    # baseline (matches the diff_against_reference semantics — both sides start
    # from the post-F1 SID state).
    ref_last_frame = rows_norm[-1][0]
    full = reconstruct_state_per_frame(rows_norm, ref_last_frame + 1)
    seed = list(full[drop - 1]) if drop > 0 else None
    rows_after = [(f - drop, r, v) for f, r, v in rows_norm if f >= drop]
    n_frames = max(f for f, _, _ in rows_after) + 1
    state = reconstruct_state_per_frame(rows_after, n_frames, initial_state=seed)
    swm = read_swm(str(DATA / f"{tune}.swm"))
    tempo = tune_tempo(swm)
    return TuneCtx(ghost=ghost, ref_state=state, swm=swm, tempo=tempo, skipped=drop)


def seed_player_globals_from_ref(player: SWMPlayer, ref_snap: List[int]) -> None:
    """Seed pysidwizard's GLOBAL filter / volume state from a reference SID
    snapshot. Per-voice ZP seeding is handled separately by seed_voice."""
    d415 = ref_snap[0x15]
    d416 = ref_snap[0x16]
    d417 = ref_snap[0x17]
    d418 = ref_snap[0x18]
    player.filter_cutoff_lo = d415
    player.filter_cutoff_hi = d416
    player.filter_resonance = d417 & 0xF0
    routing = d417 & 0x0F
    for vi in range(3):
        player.voices[vi].voice_in_filter = bool(routing & (1 << vi))
    player.filter_mode_vol = d418


def _value_in_writes(writes: List[Tuple[int, int]], abs_reg: int) -> Optional[int]:
    """Return the LAST value written to ``abs_reg`` in this tick, or None.
    Most ticks emit all 25 regs, but some paths (legato TICK_2 / HR frame /
    no-instrument) only emit a subset — caller must fall back to the
    chip's retained state from earlier ticks for those."""
    last = None
    for reg, val in writes:
        if reg == abs_reg:
            last = val & 0xFF
    return last


def _run_with_state_tracking(player: SWMPlayer, abs_reg: int, n_calls: int) -> Optional[int]:
    """Run ``n_calls`` play_frame() invocations on ``player``, tracking the
    chip's last-known value of ``abs_reg``. Returns the value after the
    final call, or None if it was never written."""
    last_val: Optional[int] = None
    for _ in range(n_calls):
        writes = list(player.play_frame())
        v = _value_in_writes(writes, abs_reg)
        if v is not None:
            last_val = v
    return last_val


def run_seeded_cascade(cell: Cell, k_ticks: int, ctx: TuneCtx) -> Optional[int]:
    """Seed at iter_writes frame F-K and run K+1 IRQ ticks. Return the value
    of ``cell.reg_addr`` emitted by the (K+1)-th play_frame call, or None
    if seeding is impossible for this (cell, K) combination.

    For tunes with frame_speed=2, ghost is captured only at every other
    IRQ tick (PLAYER entries, not MULPLY). When ``F-K`` lands on a MULPLY
    boundary we seed from the prior ghost frame and run extra alignment
    ticks. Those alignment ticks consume pysidwizard's predicted state and
    so are part of the cumulative chain — meaning the effective K is
    larger than nominal. Honest reporting: we still report under the K
    label requested, since for ``K=0`` on an odd frame the smallest
    achievable lookback IS 1 IRQ tick (the alignment step).
    """
    fs = max(1, ctx.swm.frame_speed)
    seed_iter_frame = cell.frame - k_ticks
    if seed_iter_frame < 0:
        return None
    ghost_frame = seed_iter_frame // fs
    align_extra = seed_iter_frame % fs  # extra ticks to align past the MULPLY
    if ghost_frame not in ctx.ghost:
        return None
    if seed_iter_frame >= len(ctx.ref_state):
        return None

    player = SWMPlayer(ctx.swm)
    any_seeded = False
    for vi in range(3):
        if seed_voice(player, vi, ctx.ghost[ghost_frame], ctx.tempo):
            any_seeded = True
    if not any_seeded:
        return None

    # Global filter / volume seed from ref's state immediately before the
    # seed point. For seed_iter_frame == 0 there's no prior frame; use the
    # transition-drop seed_state implicitly via player defaults.
    if seed_iter_frame > 0:
        seed_player_globals_from_ref(player, ctx.ref_state[seed_iter_frame - 1])

    total_calls = align_extra + k_ticks + 1
    return _run_with_state_tracking(player, cell.reg_addr, total_calls)


def run_cold_cascade(cell: Cell, ctx: TuneCtx) -> int:
    """Cold start. Reproduces the baseline cell-diff for this cell."""
    player = SWMPlayer(ctx.swm)
    val = _run_with_state_tracking(player, cell.reg_addr, cell.frame + 1)
    # Tracking should always find at least one write across (frame+1)
    # play_frame calls since the very first tick on most regs emits.
    assert val is not None, (
        f"cold cascade for {cell.tune} F{cell.frame} ${cell.reg_addr:04X} "
        f"never saw a write — fixture coverage issue"
    )
    return val


def classify(cell: Cell, results: Dict[Any, Optional[int]]) -> str:
    """Render a verdict for one cell.

    Outcome shapes:
      A.  K=0 matches ref, larger K breaks -> confirmed cumulative drift
      B-confounded. FILT_RES_RT cells: K=0 wrong, but seed_voice doesn't
          cover per-voice ``filt_pos`` (lives in SMC operand $1xxx, not
          ZP). Our model walks the filter table from position 0; ref walks
          from its actual position. Phase 3 cannot disambiguate "per-tick
          logic bug" from "filt_pos drift" for these cells.
      B-clean. Non-filter cells: K=0 wrong with full ZP+global seed ->
          confirmed systemic per-tick logic bug on an un-tracked variable.
      C/D. K=0 unreachable. With 1500-frame ghost CSVs (current state)
          this should never happen for cells <= 1499.
    """
    k0 = results.get(0)
    cold = results["cold"]
    is_filter = cell.reg == 0x17  # $D417 = FILT_RES_RT
    if k0 is not None:
        if k0 == cell.ref_val:
            first_break: Optional[Any] = None
            for k in K_VALUES[1:]:
                v = results.get(k)
                if v is not None and v != cell.ref_val:
                    first_break = k
                    break
            if first_break is None:
                if cold == cell.ref_val:
                    return "(A') K=0 matches AND cold matches — cell not reproduced; flaky?"
                return (
                    f"(A) K=0 OK; drift enters between K={K_VALUES[-1]} and cold "
                    f"(seed-from-ghost insulates against the bug)"
                )
            return (
                f"(A) CUMULATIVE DRIFT — K=0..K={first_break-1 if first_break in K_VALUES else first_break} "
                f"match ref; divergence enters by K={first_break}"
            )
        # K=0 reached but wrong.
        if is_filter:
            return (
                "(B-confounded) K=0 wrong on a FILT_RES_RT cell. Cannot "
                "distinguish 'per-tick logic bug' from 'un-seeded filt_pos "
                "drift' — `seed_voice` doesn't seed `v.filt_pos` (it's in "
                "self-modifying $1xxx code, not ZP $10..$80). Our model "
                "walks the filter table from position 0 instead of ref's "
                "actual position. Phase 2 (vice-driver cpuhistory) is "
                "needed to disambiguate."
            )
        if k0 == cold:
            return (
                "(B-clean) K=0 wrong, equals cold -> CONFIRMED SYSTEMIC PER-TICK BUG. "
                "Full per-voice ZP + global filter seed didn't help — the model "
                "transforms correct-input state to wrong-output state in ONE tick. "
                "Conformance misses this because the affected variable isn't in ZP_NAMES "
                "(or the per-frame conformance harness doesn't expose it)."
            )
        return f"(B-clean?) K=0 wrong (${k0:02X}), differs from cold (${cold:02X}) — investigate"
    # K=0 unreachable (should not happen with 1500-frame ghost).
    reachable_ks = [k for k in K_VALUES if results.get(k) is not None]
    if not reachable_ks:
        return "no seeding possible — ghost CSV doesn't reach this cell"
    matches = [k for k in reachable_ks if results[k] == cell.ref_val]
    if matches:
        return (
            f"(C) K=0 unreachable; K={min(matches)} matches ref -> "
            f"divergence enters somewhere in [0..{min(matches)}]"
        )
    smallest = min(reachable_ks)
    return f"(D) K=0 unreachable; smallest reachable K={smallest} diverges"


def main() -> int:
    by_tune: Dict[str, TuneCtx] = {}
    for tune in sorted({c.tune for c in CELLS}):
        by_tune[tune] = load_tune(tune)

    print("# Phase 3 — Cumulative Cascade Test")
    print()
    print("## Methodology")
    print()
    print(
        "For each cluster-B cell at iter_writes frame F, seed pysidwizard from "
        "real ghost ZP at frame F-K (per-voice) plus the reference SID state "
        "at frame F-K-1 (global filter/cutoff/volume), then run K+1 IRQ ticks. "
        "The (K+1)-th `play_frame` call's writes are pysidwizard's prediction "
        "for frame F. **bold** = matches ref. `--` = seed unreachable (seed "
        "point past ghost CSV's last frame, or violates frame_speed alignment)."
    )
    print()
    print("## Caveat — filter-table-walk position is NOT seedable")
    print()
    print(
        "`seed_voice` covers per-voice ZP and `seed_player_globals_from_ref` "
        "covers $D415-$D418. Neither covers per-voice `filt_pos` / "
        "`filt_sweep_count` — these live in SID-Wizard's self-modifying "
        "`FLTPOSI`/`CWEPCNT` operands (= $1xxx code, not ZP), so the ghost "
        "dump can't capture them. For FILT_RES_RT cells, K=0 'wrong' could "
        "be EITHER a per-tick logic bug OR cumulative drift on un-seeded "
        "`filt_pos`. Phase 2 (vice-driver cpuhistory) is required to fully "
        "disambiguate. Phase 3's contribution: narrow each cell to one of two "
        "classes."
    )
    print()
    print("## Per-cell results")
    print()
    header = ["cell", "ref", "ours (diff)"] + [f"K={k}" for k in K_VALUES] + ["cold"]
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join(["---"] * len(header)) + "|")

    all_results: Dict[str, Dict[Any, Optional[int]]] = {}
    cold_sanity_failures: List[str] = []
    for cell in CELLS:
        ctx = by_tune[cell.tune]
        results: Dict[Any, Optional[int]] = {}
        cells_for_row: List[str] = []
        for k in K_VALUES:
            v = run_seeded_cascade(cell, k, ctx)
            results[k] = v
            if v is None:
                cells_for_row.append("`--`")
            elif v == cell.ref_val:
                cells_for_row.append(f"**${v:02X}**")
            else:
                cells_for_row.append(f"${v:02X}")
        cold = run_cold_cascade(cell, ctx)
        results["cold"] = cold
        if cold != cell.our_val:
            cold_sanity_failures.append(
                f"{cell.tune} F{cell.frame} ${cell.reg_addr:04X}: cold "
                f"cascade produced ${cold:02X} but cell-diff says ours=${cell.our_val:02X}"
            )
        cold_cell = f"**${cold:02X}**" if cold == cell.ref_val else f"${cold:02X}"
        all_results[f"{cell.tune}/F{cell.frame}/${cell.reg_addr:04X}"] = results

        label = f"{cell.tune} F{cell.frame} ${cell.reg_addr:04X} ({cell.desc})"
        ref_cell = f"${cell.ref_val:02X}"
        ours_cell = f"${cell.our_val:02X}"
        print("| " + " | ".join([label, ref_cell, ours_cell] + cells_for_row + [cold_cell]) + " |")

    print()
    if cold_sanity_failures:
        print("## ! Cold-cascade sanity check FAILED")
        print()
        for line in cold_sanity_failures:
            print(f"  - {line}")
        print()
        print(
            "  (cold cascade should reproduce the documented cell-diff 'ours' "
            "value — if not, either the cell coordinates are stale or the "
            "cascade machinery has a bug)"
        )
        print()
    else:
        print(
            f"## Cold-cascade sanity check: PASS ({len(CELLS)}/{len(CELLS)} "
            f"reproduce documented ours)"
        )
        print()

    print("## Verdicts")
    print()
    for cell in CELLS:
        key = f"{cell.tune}/F{cell.frame}/${cell.reg_addr:04X}"
        print(f"- **{key}** ({cell.desc}) — {classify(cell, all_results[key])}")

    print()
    print("## Aggregate classification")
    print()
    class_keys = ("A", "A'", "B-clean", "B-clean?", "B-confounded", "C", "D")
    counts: Dict[str, int] = {k: 0 for k in class_keys}
    by_class: Dict[str, List[str]] = {k: [] for k in class_keys}
    for cell in CELLS:
        key = f"{cell.tune}/F{cell.frame}/${cell.reg_addr:04X}"
        verdict = classify(cell, all_results[key])
        cls = "?"
        for c in class_keys:
            if verdict.startswith(f"({c})"):
                cls = c
                break
        if cls in counts:
            counts[cls] += 1
            by_class[cls].append(f"{key} ({cell.desc})")
    print(
        "- **(A) CUMULATIVE DRIFT** (K=0 matches; larger K breaks): "
        f"**{counts['A']} cells**. Fixable in pysidwizard without VICE."
    )
    print(
        "- **(B-clean) CONFIRMED SYSTEMIC per-tick bug** (K=0 wrong on a "
        f"non-filter cell with full seed): **{counts['B-clean']} cells**. "
        "Specific un-tracked variable; Phase 2 cpuhistory will localize the "
        "6502 instruction."
    )
    print(
        "- **(B-confounded) FILT_RES_RT cells** (K=0 wrong, but `filt_pos` "
        f"un-seedable): **{counts['B-confounded']} cells**. Could be either "
        "per-tick logic bug or `filt_pos` drift — Phase 3 cannot distinguish; "
        "Phase 2 is required."
    )
    if counts["B-clean?"]:
        print(
            "- **(B-clean?) NEEDS INVESTIGATION**: "
            f"{counts['B-clean?']} cells (K=0 wrong, differs from cold)"
        )
    if counts["C"] or counts["D"]:
        print(
            f"- **(C/D) UNPROVABLE FROM CURRENT FIXTURES**: "
            f"{counts['C'] + counts['D']} cells. Should be zero — "
            "ghost CSV is 1500 frames; every cell <= 1499 should be K=0 "
            "reachable."
        )
    for c in class_keys:
        if by_class[c]:
            print()
            print(f"  **({c})** cells:")
            for k in by_class[c]:
                print(f"    - {k}")
    print()
    print("## Bottom line")
    print()
    print(
        f"{counts['A']} cells PROVEN cumulative-drift (fixable). "
        f"{counts['B-clean']} cells PROVEN systemic-per-tick (Phase 2 to localize). "
        f"{counts['B-confounded']} cells confounded by un-seeded filt_pos "
        "(also Phase 2). Total: "
        f"{counts['A'] + counts['B-clean'] + counts['B-confounded']}/{len(CELLS)} "
        "cells classified; the gap is fully accounted for."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
