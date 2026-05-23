"""Frame-0 trace: cold-seed pysidwizard, run one play_frame(), dump
per-voice state + emitted SID writes alongside the reference frame-0
SID state. Surfaces the row-0-trigger mismatch on bronkosaurus +
rain8580 (see [[pysidwizard-final-gap]]).

Conformance skips row-trigger frames (SPDCNT in {tempo, 1, 2}), so the
frame-0 divergence is invisible there. This tool fills that gap.

The reference CSV stores frame 0 sparsely (only registers that differ
from the post-INITER zero state). We treat any register not listed as
$00.

CLI: ``PYTHONPATH=src python3 tools/frame0_trace.py``
     ``PYTHONPATH=src python3 tools/frame0_trace.py --tune bronkosaurus``
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from pysidwizard.model import PlayPattern, SequenceCommand, Transpose  # noqa: E402
from pysidwizard.player import SID_REG_BASE, SWMPlayer, VoiceState  # noqa: E402
from pysidwizard.reader import read_swm  # noqa: E402
from tests._diff_harness import (  # noqa: E402
    detect_transition_end,
    load_reference_csv,
    normalize_reference,
    reconstruct_state_per_frame,
)
from tests._swm_cache import swm_path as _fetch_swm  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

TUNES = ["flashitback", "bronkosaurus", "euphoria", "rain8580"]

REG_NAMES = {
    0x00: "v0_FREQ_LO",
    0x01: "v0_FREQ_HI",
    0x02: "v0_PW_LO",
    0x03: "v0_PW_HI",
    0x04: "v0_CTRL",
    0x05: "v0_AD",
    0x06: "v0_SR",
    0x07: "v1_FREQ_LO",
    0x08: "v1_FREQ_HI",
    0x09: "v1_PW_LO",
    0x0A: "v1_PW_HI",
    0x0B: "v1_CTRL",
    0x0C: "v1_AD",
    0x0D: "v1_SR",
    0x0E: "v2_FREQ_LO",
    0x0F: "v2_FREQ_HI",
    0x10: "v2_PW_LO",
    0x11: "v2_PW_HI",
    0x12: "v2_CTRL",
    0x13: "v2_AD",
    0x14: "v2_SR",
    0x15: "FLT_CUTOFF_LO",
    0x16: "FLT_CUTOFF_HI",
    0x17: "FLT_RES_RT",
    0x18: "MODE_VOL",
}


def load_ref_aligned_states(ref_path: Path) -> Tuple[int, List[int], List[int]]:
    """Load the reference, F1-align it, and return
    ``(skipped, seed_state, frame0_state)`` where:

    - ``skipped`` is the number of pre-song frames dropped (= same as
      ``test_player_reference``'s alignment).
    - ``seed_state`` is the SID register state at the end of the last
      dropped frame — the baseline both sides start from at frame 0.
    - ``frame0_state`` is the cumulative reference SID state at the
      FIRST compared frame (= our player's frame 0 should match this).
    """
    rows = load_reference_csv(ref_path)
    _start_offset, ref = normalize_reference(rows)
    del _start_offset
    skipped = detect_transition_end(ref)
    last_frame = ref[-1][0]
    state_full = reconstruct_state_per_frame(ref, last_frame + 1)
    if skipped <= 0:
        seed = [0] * 25
    else:
        seed = list(state_full[skipped - 1])
    frame0 = list(state_full[skipped])
    return skipped, seed, frame0


def render_row(row) -> str:
    if row is None:
        return "<no row 0>"
    parts: List[str] = []
    if row.note is None:
        parts.append("note=---")
    else:
        parts.append(f"note=${row.note:02X}({row.note:3d})")
    if row.instrument is not None:
        parts.append(f"ins=${row.instrument:02X}")
    if row.fx is not None:
        if row.fx_value is not None:
            parts.append(f"fx=${row.fx:02X} val=${row.fx_value:02X}")
        else:
            parts.append(f"fx=${row.fx:02X}")
    return " ".join(parts)


def render_seq_cmd(cmd: SequenceCommand) -> str:
    if isinstance(cmd, PlayPattern):
        return f"PlayPattern({cmd.pattern})"
    if isinstance(cmd, Transpose):
        return f"Transpose({cmd.semitones:+d})"
    return type(cmd).__name__


def describe_instrument(ins) -> List[str]:
    if ins is None:
        return ["<no instrument>"]
    wf = ins.wf_table
    pw = ins.pw_table
    ft = ins.filter_table
    return [
        f"name='{ins.name_str()}' control=${ins.control:02X}"
        f" octshift={ins.octave_shift:+d} default_chord={ins.default_chord}"
        f" first_wf=${ins.first_waveform:02X}",
        f"  HR ADSR=${ins.hr_attack:X}/{ins.hr_decay:X}/"
        f"{ins.hr_sustain:X}/{ins.hr_release:X}"
        f"  post-HR ADSR=${ins.attack:X}/{ins.decay:X}/"
        f"{ins.sustain:X}/{ins.release:X}",
        f"  vibrato=${ins.vibrato:02X} vib_delay=${ins.vibrato_delay:02X}"
        f" arp_speed=${ins.arp_speed:02X}",
        f"  gateoff wf/pw/filt=${ins.gateoff_wf:02X}/"
        f"${ins.gateoff_pw:02X}/${ins.gateoff_filt:02X}",
        "  wf_table[0:6] = "
        + " ".join(f"${b:02X}" for b in wf[:6])
        + (f" .. ({len(wf)}B)" if len(wf) > 6 else ""),
        "  pw_table[0:6] = "
        + " ".join(f"${b:02X}" for b in pw[:6])
        + (f" .. ({len(pw)}B)" if len(pw) > 6 else ""),
        "  filter_table[0:6] = "
        + " ".join(f"${b:02X}" for b in ft[:6])
        + (f" .. ({len(ft)}B)" if len(ft) > 6 else ""),
    ]


def describe_voice_state(v: VoiceState) -> List[str]:
    return [
        f"  note=${v.note:02X}({v.note:3d}) octshift={v.octave_shift:+d}"
        f" transpose={v.transpose:+d} dpitch={(v.note + v.octave_shift + v.transpose) & 0x7F:3d}"
        f" instrument_idx={v.instrument_idx}",
        f"  hr_timer={v.hr_timer} in_hr_this_frame={v.in_hr_this_frame}"
        f" speed_counter={v.speed_counter} triggered={v.triggered}",
        f"  wf_pos={v.wf_pos} pw_pos={v.pw_pos} filt_pos={v.filt_pos}"
        f" wf_pitch_carry={v.wf_pitch_carry}"
        f" wf_did_pitch_write={v.wf_did_pitch_write}"
        f" arp_speed_counter=${v.arp_speed_counter & 0xFF:02X}",
        f"  sid_freq=${v.sid_freq_hi:02X}{v.sid_freq_lo:02X}"
        f" sid_pw=${v.sid_pw_hi & 0x0F:X}{v.sid_pw_lo:02X}"
        f" sid_ctrl=${v.sid_ctrl:02X}"
        f" sid_ad=${v.sid_ad:02X} sid_sr=${v.sid_sr:02X}",
        f"  voice_in_filter={v.voice_in_filter}"
        f" ptn_gate=${v.ptn_gate:02X}"
        f" pw_kbd_track=${v.pw_kbd_track:02X}"
        f" detune={v.detune}",
        f"  vib delay={v.vibrato_delay_counter} cnt={v.vibrato_freq_counter}"
        f" period={v.vibrato_period} offset={v.vibrato_offset}"
        f" slide_vib=${v.slide_vib & 0xFF:02X}",
        f"  current_chord={v.current_chord} chord_pos={v.chord_pos}"
        f" pending_row={'<set>' if v.pending_row is not None else 'None'}",
    ]


def trace_tune(tune: str) -> int:
    """Run one play_frame() for ``tune`` and print a diff against ref.

    Returns the number of diverging registers (frame 0 only).
    """
    swm_path = _fetch_swm(tune)
    ref_path = FIXTURES / f"{tune}.reference.csv"
    if not swm_path.exists():
        print(f"[{tune}] missing {swm_path}")
        return -1
    if not ref_path.exists():
        print(f"[{tune}] missing {ref_path}")
        return -1

    swm = read_swm(swm_path)
    skipped, seed_state, ref0_state = load_ref_aligned_states(ref_path)

    if swm.subtune_tempos:
        tl, tr = swm.subtune_tempos[0]
    else:
        tl, tr = 6, 6
    print("=" * 78)
    print(
        f"=== {tune} (tempo {tl & 0x7F}/{tr & 0x7F},"
        f" frame_speed {swm.frame_speed},"
        f" ref skipped={skipped} F1 frame(s))"
    )
    print("=" * 78)

    print("\n-- Sequences (orderlist) --")
    for vi, seq in enumerate(swm.sequences):
        head = ", ".join(render_seq_cmd(c) for c in seq[:4])
        print(f"  v{vi}: {head}{' ...' if len(seq) > 4 else ''}")

    player = SWMPlayer(swm)

    print("\n-- Row 0 per voice (post-_advance_row pending) --")
    for vi, v in enumerate(player.voices):
        prow = v.pending_row
        print(
            f"  v{vi}: pending={render_row(prow)}"
            f" pattern={'<set>' if v.pattern is not None else 'None'}"
            f" transpose={v.transpose:+d}"
        )

    print("\n-- Instruments referenced at row 0 --")
    seen: set = set()
    for vi, v in enumerate(player.voices):
        prow = v.pending_row
        ins_idx = prow.instrument if prow is not None else None
        if ins_idx is None or ins_idx in seen:
            continue
        seen.add(ins_idx)
        if 1 <= ins_idx <= len(swm.instruments):
            ins = swm.instruments[ins_idx - 1]
            print(f"  ins[{ins_idx}] (touched by v{vi}):")
            for line in describe_instrument(ins):
                print(f"    {line}")

    writes = player.play_frame()
    ours_writes_frame0: Dict[int, int] = {}
    for reg, val in writes:
        r = reg - SID_REG_BASE
        if 0 <= r <= 0x18:
            ours_writes_frame0[r] = val & 0xFF
    # Cumulative ours state at frame 0 = seed (post-F1 SID baseline)
    # overlaid with our frame-0 writes. This matches the comparison
    # `diff_against_reference` performs.
    ours0_state = list(seed_state)
    for r, v in ours_writes_frame0.items():
        ours0_state[r] = v

    print("\n-- Per-voice state after play_frame() --")
    for vi, v in enumerate(player.voices):
        print(f"  v{vi}:")
        for line in describe_voice_state(v):
            print(f"  {line}")

    print("\n-- SID writes emitted (frame 0, in order) --")
    for reg, val in writes:
        r = reg - SID_REG_BASE
        label = REG_NAMES.get(r, f"reg${r:02X}")
        print(f"  ${reg:04X} ({label:13s}) = ${val & 0xFF:02X}")

    print("\n-- Side-by-side cumulative state @ aligned frame 0 --")
    print(
        "  (state = post-F1 seed + this frame's writes; this is what"
        " test_player_reference compares)"
    )
    print(f"  {'reg':5s} {'label':16s} {'seed':>5s} {'ref':>5s}" f" {'ours':>5s}  diff")
    print(f"  {'-'*5} {'-'*16} {'-'*5} {'-'*5} {'-'*5}  ----")
    n_diff = 0
    for r in range(0x19):
        seed_v = seed_state[r]
        ref_v = ref0_state[r]
        our_v = ours0_state[r]
        if ref_v == our_v and ref_v == 0 and seed_v == 0:
            continue
        marker = "  OK" if ref_v == our_v else "  **"
        if ref_v != our_v:
            n_diff += 1
        label = REG_NAMES.get(r, f"reg${r:02X}")
        print(f"  ${r:02X}   {label:16s} ${seed_v:02X}   ${ref_v:02X}  " f"${our_v:02X}   {marker}")

    print(f"\n[{tune}] frame-0 register divergences: {n_diff}")
    return n_diff


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--tune",
        action="append",
        choices=TUNES,
        help="Tune(s) to trace. Repeat for multiple. Default: all four.",
    )
    args = ap.parse_args()
    tunes = args.tune or TUNES
    total = 0
    for tune in tunes:
        n = trace_tune(tune)
        if n > 0:
            total += n
        print()
    print(f"TOTAL frame-0 divergences across {len(tunes)} tune(s): {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
