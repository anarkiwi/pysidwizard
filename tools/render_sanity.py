"""Render side-by-side WAVs for sanity-checking the player.

For each fixture tune emit two WAVs:
    {tune}.ours.wav  — pysidwizard's SWMPlayer output
    {tune}.ref.wav   — SID-Wizard's captured CSV played back

Both go through the same pyresidfp emulator at the same sample rate
so the two files are directly A/B comparable. Default duration 30 s.

CLI::

    python3 tools/render_sanity.py
    python3 tools/render_sanity.py --seconds 20 --out /tmp/sanity --model MOS8580
"""

from __future__ import annotations

import argparse
import csv
import sys
import wave
from datetime import timedelta
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pysidwizard.player import (  # noqa: E402
    PAL_CYCLES_PER_FRAME,
    iter_writes,
)
from pysidwizard.reader import read_swm  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TUNES = ("flashitback", "bronkosaurus", "euphoria", "rain8580")


def _detect_ref_transition(rows: List[Tuple[int, int, int]]) -> int:
    """How many leading frames of the ref CSV to drop -- mirrors
    ``player_diff.detect_transition_end`` so the rendered ref aligns
    with our player's frame 0 (= first row trigger)."""
    from tests._diff_harness import detect_transition_end

    return detect_transition_end(rows)


def render_writes_to_wav(
    writes_by_frame: List[List[Tuple[int, int]]],
    out_wav: Path,
    frame_speed: int,
    model_name: str,
) -> int:
    import numpy as np
    from pyresidfp import SoundInterfaceDevice, WritableRegister
    from pyresidfp.sound_interface_device import ChipModel  # type: ignore

    sid = SoundInterfaceDevice(model=getattr(ChipModel, model_name))
    sid.reset()
    for r in range(25):
        sid.write_register(WritableRegister(r), 0)
    sid.clock(timedelta(seconds=0.05))

    cycles_per_sec = sid.clock_frequency
    cycles_per_frame = PAL_CYCLES_PER_FRAME // max(1, frame_speed)
    seconds_per_frame = cycles_per_frame / cycles_per_sec

    chunks = []
    for frame_writes in writes_by_frame:
        for r, v in frame_writes:
            sid.write_register(WritableRegister(r), v)
        samples = sid.clock(timedelta(seconds=seconds_per_frame))
        chunks.append(np.asarray(samples, dtype=np.int16))

    audio = np.concatenate(chunks) if chunks else np.array([], dtype=np.int16)
    sr = int(sid.sampling_frequency)
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(audio.tobytes())
    return len(audio) // sr if sr else 0


def render_tune(tune: str, seconds: float, out_dir: Path, model: str) -> None:
    swm_path = REPO_ROOT / "tests" / "data" / f"{tune}.swm"
    ref_csv = REPO_ROOT / "tests" / "fixtures" / f"{tune}.reference.csv"

    swm = read_swm(str(swm_path))
    cycles_per_frame = PAL_CYCLES_PER_FRAME // max(1, swm.frame_speed)
    # Convert seconds to player-frame count.
    # PAL clock ~ 985_248 Hz; frame_speed=2 doubles the player-tick rate.
    n_frames = int(seconds * 985248 / cycles_per_frame)

    # --- ours --------------------------------------------------------------
    ours_by_frame: List[List[Tuple[int, int]]] = [[] for _ in range(n_frames)]
    for f, r, v in iter_writes(swm, n_frames, dedupe=True):
        if f < n_frames:
            ours_by_frame[f].append((r, v))
    ours_path = out_dir / f"{tune}.ours.wav"
    duration = render_writes_to_wav(ours_by_frame, ours_path, swm.frame_speed, model)
    print(f"  {tune}.ours.wav: {duration}s ({n_frames} frames @ {swm.frame_speed}x)")

    # --- ref ---------------------------------------------------------------
    if not ref_csv.exists():
        print(f"  (no {ref_csv.name}; skipping ref render)")
        return
    ref_rows: List[Tuple[int, int, int]] = []
    with open(ref_csv) as fp:
        for row in csv.DictReader(fp):
            ref_rows.append((int(row["frame"]), int(row["reg"]), int(row["value"])))
    # Drop F1-transition frames so ref's frame 0 = our frame 0 (= first
    # row trigger / STRTSND).
    skip = _detect_ref_transition(ref_rows)
    ref_by_frame: List[List[Tuple[int, int]]] = [[] for _ in range(n_frames)]
    for f, r, v in ref_rows:
        rf = f - skip
        if 0 <= rf < n_frames:
            ref_by_frame[rf].append((r & 0x1F, v))
    ref_path = out_dir / f"{tune}.ref.wav"
    duration = render_writes_to_wav(ref_by_frame, ref_path, swm.frame_speed, model)
    print(f"  {tune}.ref.wav:  {duration}s (skipped {skip} F1-transition frames)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--tune", choices=TUNES, default=None, help="render a single tune; default is all 4"
    )
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--model", default="MOS8580", choices=["MOS6581", "MOS8580"])
    ap.add_argument("--out", type=Path, default=Path("/tmp/pysidwizard-sanity"))
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    tunes = [args.tune] if args.tune else list(TUNES)
    print(f"Rendering {len(tunes)} tune(s), {args.seconds}s each, model={args.model}")
    print(f"Output dir: {args.out}")
    for tune in tunes:
        print(f"\n[{tune}]")
        render_tune(tune, args.seconds, args.out, args.model)
    print(
        f"\nDone. A/B listen: e.g. `aplay {args.out}/flashitback.ours.wav` "
        f"vs `aplay {args.out}/flashitback.ref.wav`."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
