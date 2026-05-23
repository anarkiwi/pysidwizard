"""Single-tick prediction harness: for each ghost-frame K, seed
pysidwizard's voice state from ghost K, run ONE PLAYER tick, and
verify the result matches ghost K+1.

This isolates the player's per-tick state-transition logic from the
"alignment" problem: each ghost frame becomes an independent test
case for "given THIS input state, predict THIS output state".

Currently focused on euphoria v0 — the voice with the worst FREQ_LO
drift. Tracks a subset of ZP variables that our model can both read
from ghost frames AND drive to a known state for prediction.

Output: one row per ghost frame K -> (frame, var, ref_K+1, our_K+1).

CLI: ``python tools/tick_predict.py --tune euphoria --voice 0``
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

INST_WF_TABLE_POS = 0x10
PER_VOICE_STRIDE = 7


# ZP address (v0 base) -> variable name. Used for reporting only.
ZP_NAMES = {
    0x10: "FREQLO",
    0x11: "FREQHI",
    0x12: "PWLOGHO",
    0x13: "PWHIGHO",
    0x14: "WFGHOST",
    0x15: "PTNGATE",
    0x16: "PWEEPCNT",
    0x25: "PACKCNT",
    0x26: "SPDCNT",
    0x27: "SEQPOS",
    0x28: "PTNPOS",
    0x29: "WFTPOS",
    0x2A: "PWTPOS",
    0x2B: "ARPSCNT",
    0x3A: "CURPTN",
    0x3B: "CURNOT",
    0x3C: "DPITCH",
    0x3D: "CURIFX",
    0x3E: "CURINS",
    0x3F: "CURFX2",
    0x40: "CURVAL",
    0x4F: "SLIDEVIB",
    0x50: "FREQMODL",
    0x51: "FREQMODH",
    0x52: "VIDELCNT",
    0x53: "VIBFREQU",
    0x54: "VIBRACNT",
    0x55: "TRANSP",
    0x68: "CURCHORD",
    0x69: "CHORDPOS",
}


def load_ghost(path: Path) -> Dict[int, Dict[int, int]]:
    out: Dict[int, Dict[int, int]] = {}
    with open(path, newline="") as fp:
        for row in csv.DictReader(fp):
            f = int(row["frame"])
            a = int(row["addr"])
            v = int(row["value"])
            out.setdefault(f, {})[a] = v
    return out


def addr(base: int, voice: int) -> int:
    return base + voice * PER_VOICE_STRIDE


def seed_voice(player: SWMPlayer, voice: int, ghost: Dict[int, int]) -> None:
    """Inject ghost ZP values into ``player.voices[voice]``."""
    v = player.voices[voice]
    # FREQLO/FREQHI/PWLOGHO/PWHIGHO ZP are PRE-mask values driving the
    # SID writes — map to our pre-mask v.pw_hi/v.pw_lo (NOT sid_pw_hi
    # which is the masked output).
    v.sid_freq_lo = ghost.get(addr(0x10, voice), 0)
    v.sid_freq_hi = ghost.get(addr(0x11, voice), 0)
    v.pw_lo = ghost.get(addr(0x12, voice), 0)
    v.pw_hi = ghost.get(addr(0x13, voice), 0)
    v.sid_pw_lo = v.pw_lo
    v.sid_pw_hi = v.pw_hi & 0x0F
    v.sid_ctrl = ghost.get(addr(0x14, voice), 0)
    v.ptn_gate = ghost.get(addr(0x15, voice), 0xFF)
    v.pw_sweep_count = ghost.get(addr(0x16, voice), 0)
    # SID-Wizard's SPDCNT cycles 1..tempo at $1003 entry; our model
    # keeps speed_counter in 0..tempo-1. Translate ghost SPDCNT==tempo
    # -> our speed_counter=0 (= just-wrapped, next tick will be row
    # trigger). Other values pass through.
    sc = ghost.get(addr(0x26, voice), 0)
    # Tempo lookup: we don't have the tune's tempo here easily, but
    # for euphoria + most fixtures it's 5 (= "first row trigger
    # observable at SPDCNT=5"). Treat any sc>=5 as wrapped.
    tempo = 5
    if sc >= tempo:
        v.speed_counter = 0
    else:
        v.speed_counter = sc
    # WFTPOS, PWTPOS are absolute offsets in instrument data; convert
    # to our 0-relative positions.
    wftpos = ghost.get(addr(0x29, voice), 0x10)
    v.wf_pos = max(0, wftpos - INST_WF_TABLE_POS)
    # PWTPOS conversion requires knowing the instrument first.
    arpscnt = ghost.get(addr(0x2B, voice), 0)
    # Treat $80..$FF as negative (= just-started arp counter).
    v.arp_speed_counter = arpscnt if arpscnt < 0x80 else arpscnt
    # In our model v.note is the "last pitched CURNOT", and compose
    # uses ``v.note + octave_shift + transpose`` to derive what
    # SID-Wizard's DPITCH would be. Seed v.note from DPITCH instead
    # of CURNOT, since CURNOT==$00 on NOP rows (= no note) but
    # DPITCH carries forward the last pitched value.
    curins = ghost.get(addr(0x3E, voice), 0)
    if 1 <= curins <= len(player.swm.instruments):
        v.instrument = player.swm.instruments[curins - 1]
        v.instrument_idx = curins
        v.octave_shift = v.instrument.octave_shift
    dpitch = ghost.get(addr(0x3C, voice), 0)
    transp = ghost.get(addr(0x55, voice), 0)
    if transp >= 0x80:
        transp -= 0x100
    # v.note + octave_shift + transpose == dpitch  =>  v.note = dpitch - octave_shift - transpose
    v.note = (dpitch - v.octave_shift - transp) & 0x7F
    # Now we know the instrument; can convert PWTPOS.
    pwtpos = ghost.get(addr(0x2A, voice), 0x10)
    if v.instrument:
        pw_table_start = INST_WF_TABLE_POS + len(v.instrument.wf_table) + 1
        v.pw_pos = max(0, pwtpos - pw_table_start)
    # Slide / vibrato state.
    v.slide_vib = ghost.get(addr(0x4F, voice), 0)
    v.vibrato_freqmod_lo = ghost.get(addr(0x50, voice), 0)
    v.vibrato_freqmod_hi = ghost.get(addr(0x51, voice), 0)
    videl = ghost.get(addr(0x52, voice), 0)
    # VIDELCNT is signed: $80..$FF = negative (= delay done).
    v.vibrato_delay_counter = videl - 0x100 if videl >= 0x80 else videl
    v.vibrato_period = ghost.get(addr(0x53, voice), 0)
    v.vibrato_freq_counter = ghost.get(addr(0x54, voice), 0)
    v.transpose = ghost.get(addr(0x55, voice), 0)
    if v.transpose >= 0x80:
        v.transpose -= 0x100
    # Chord state.
    curchord = ghost.get(addr(0x68, voice), 0)
    if 1 <= curchord <= len(player._chord_starts):
        v.current_chord = curchord
        # CHORDPOS in ZP is absolute byte offset into CHORDS table.
        v.chord_pos = ghost.get(addr(0x69, voice), 0)
    # Triggered flag so emit doesn't skip.
    v.triggered = True
    v.hr_timer = 0
    # Reconstruct vibrato_offset from ghost FREQLO/FREQHI ZP. In
    # SID-Wizard FREQLO/HI ZP = NOTE_FREQ[note] + cumulative vibrato
    # mutation. Our model holds vibrato_offset separately. Derive:
    #   offset = ghost_freq - NOTE_FREQ[note]
    # NOTE_FREQ[note] is what the LAST WF arp ABS/REL would have
    # written. Since we don't know the last arp output, we approximate
    # by using v.note + transpose + octave_shift (= what compose
    # would compute for REL +0).
    from pysidwizard.player import NOTE_FREQ_HI, NOTE_FREQ_LO

    ghost_freq = ghost.get(addr(0x11, voice), 0) << 8 | ghost.get(addr(0x10, voice), 0)
    play_note = max(0, min(len(NOTE_FREQ_LO) - 1, v.note + v.octave_shift + v.transpose))
    base_freq = (NOTE_FREQ_HI[play_note] << 8) | NOTE_FREQ_LO[play_note]
    v.vibrato_offset = (ghost_freq - base_freq) & 0xFFFF


def snapshot_voice_zp(player: SWMPlayer, voice: int) -> Dict[int, int]:
    """Read back ZP-equivalent state from our model."""
    from pysidwizard.player import NOTE_FREQ_HI, NOTE_FREQ_LO

    v = player.voices[voice]
    out: Dict[int, int] = {}
    # FREQLO/FREQHI ZP = base freq (from last WF arp ABS/REL) + cumulative
    # vibrato mutation. WITHOUT DETUNER (= applied at WRPITCH only) and
    # WITHOUT wf_pitch_carry (= applied at WRPITCH only).
    if v.wf_arp_absolute:
        note = v.wf_arp_pitch & 0x7F
    else:
        note = v.note + v.wf_arp_pitch + v.transpose + v.octave_shift
    note = max(0, min(len(NOTE_FREQ_LO) - 1, note))
    zp_freq = (NOTE_FREQ_HI[note] << 8) | NOTE_FREQ_LO[note]
    zp_freq = (zp_freq + v.vibrato_offset) & 0xFFFF
    out[addr(0x10, voice)] = zp_freq & 0xFF
    out[addr(0x11, voice)] = (zp_freq >> 8) & 0xFF
    # Compare to ghost PWLOGHO / PWHIGHO ZP — those are the FULL
    # pre-mask values (v.pw_lo / v.pw_hi), not the SID-masked outputs.
    out[addr(0x12, voice)] = v.pw_lo & 0xFF
    out[addr(0x13, voice)] = v.pw_hi & 0xFF
    out[addr(0x14, voice)] = v.sid_ctrl & 0xFF
    out[addr(0x15, voice)] = v.ptn_gate & 0xFF
    out[addr(0x16, voice)] = v.pw_sweep_count & 0xFF
    # SPDCNT cycle: ghost shows 1..tempo at $1003 entry. Our model's
    # post-tick speed_counter is in 0..tempo-1, with 0 corresponding
    # to ghost's tempo (= the just-wrapped value).
    sc = v.speed_counter
    if sc == 0:
        # SID-Wizard's SPDCNT stays at tempo until next tick's sbc-wrap.
        tempo = v.tempo_left if v.tempo_toggle == 0 else v.tempo_right
        sc = (tempo & 0x7F) or 6
    out[addr(0x26, voice)] = sc & 0xFF
    out[addr(0x29, voice)] = (INST_WF_TABLE_POS + v.wf_pos) & 0xFF if v.instrument else 0
    if v.instrument:
        pw_table_start = INST_WF_TABLE_POS + len(v.instrument.wf_table) + 1
        out[addr(0x2A, voice)] = (pw_table_start + v.pw_pos) & 0xFF
    else:
        out[addr(0x2A, voice)] = 0
    out[addr(0x2B, voice)] = v.arp_speed_counter & 0xFF
    # CURNOT is not maintained separately from DPITCH in our model;
    # snapshot it as ghost CURNOT-equivalent only when v.note hasn't
    # been "lost" to NOP-row clearing. For test purposes, we just
    # echo the seed-time CURNOT here (= no per-tick prediction).
    # We'd need to add v.curnot field to track row-as-loaded.
    out[addr(0x3E, voice)] = v.instrument_idx if v.instrument_idx else 0
    out[addr(0x4F, voice)] = v.slide_vib & 0xFF
    out[addr(0x50, voice)] = v.vibrato_freqmod_lo & 0xFF
    out[addr(0x51, voice)] = v.vibrato_freqmod_hi & 0xFF
    out[addr(0x52, voice)] = v.vibrato_delay_counter & 0xFF
    out[addr(0x53, voice)] = v.vibrato_period & 0xFF
    out[addr(0x54, voice)] = v.vibrato_freq_counter & 0xFF
    out[addr(0x55, voice)] = v.transpose & 0xFF
    out[addr(0x68, voice)] = v.current_chord & 0xFF
    out[addr(0x69, voice)] = v.chord_pos & 0xFF
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tune", default="euphoria")
    ap.add_argument("--voice", type=int, default=0)
    ap.add_argument("--ghost", type=Path)
    ap.add_argument(
        "--start-frame",
        type=int,
        default=1,
        help="first ghost frame to use as seed",
    )
    ap.add_argument(
        "--max-frames",
        type=int,
        default=30,
        help="how many seed-and-predict cycles to run",
    )
    ap.add_argument(
        "--report-all",
        action="store_true",
        help="show all divergences, not just the first",
    )
    args = ap.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tests._swm_cache import swm_path as _fetch_swm

    swm_path = _fetch_swm(args.tune)
    ghost_path = args.ghost or Path(f"/tmp/{args.tune}_ghost.csv")
    if not ghost_path.exists():
        print(f"ghost not found: {ghost_path}", file=sys.stderr)
        return 2

    ghost = load_ghost(ghost_path)
    n_ghost = max(ghost) + 1

    swm = read_swm(str(swm_path))
    frame_step = max(1, swm.frame_speed)
    print(f"tune={args.tune} voice={args.voice} frame_speed={swm.frame_speed}")
    print(f"running {args.max_frames} single-tick predictions from frame {args.start_frame}")

    # Track per-variable hit/miss counts.
    var_hits: Dict[str, int] = {}
    var_misses: Dict[str, int] = {}

    first_div_frame: Optional[int] = None
    first_div_details: List[str] = []

    for start in range(
        args.start_frame, min(args.start_frame + args.max_frames, n_ghost - frame_step)
    ):
        # Skip row-trigger ticks for now — our seed can't reconstruct
        # pattern_row from PTNPOS, so we can only test mid-row
        # predictions. A row trigger has SPDCNT==tempo at entry.
        seed_sc = ghost[start].get(addr(0x26, args.voice), 0)
        if seed_sc >= 5:  # euphoria tempo = 5
            continue
        # Seed a fresh player from ghost frame `start`.
        player = SWMPlayer(swm)
        seed_voice(player, args.voice, ghost[start])
        # Run one PLAYER tick worth of play_frames.
        for _ in range(frame_step):
            player.play_frame()
        # Snapshot our predicted state.
        our_state = snapshot_voice_zp(player, args.voice)
        # Compare against ghost[start + 1].
        ref_state = ghost[start + 1]
        for a, our_val in our_state.items():
            if a not in ref_state:
                continue
            ref_val = ref_state[a]
            base = a - args.voice * PER_VOICE_STRIDE
            name = ZP_NAMES.get(base, f"?${a:02X}")
            if our_val != ref_val:
                var_misses[name] = var_misses.get(name, 0) + 1
                if first_div_frame is None:
                    first_div_frame = start
                if start == first_div_frame:
                    first_div_details.append(
                        f"  ${a:02X} {name}_v{args.voice}: ref=${ref_val:02X} ours=${our_val:02X}"
                    )
                if args.report_all:
                    print(
                        f"frm{start:3d}->frm{start + 1:3d}  ${a:02X} "
                        f"{name:<10}_v{args.voice}: "
                        f"ref=${ref_val:02X} ours=${our_val:02X}"
                    )
            else:
                var_hits[name] = var_hits.get(name, 0) + 1

    print()
    if first_div_frame is None:
        print(f"All {args.max_frames} single-tick predictions matched perfectly!")
    else:
        print(f"FIRST divergence at seed-frame {first_div_frame} -> frame {first_div_frame + 1}:")
        for d in first_div_details:
            print(d)

    print()
    print("Per-variable summary (hits / misses):")
    all_vars = sorted(set(var_hits) | set(var_misses))
    for var in all_vars:
        h = var_hits.get(var, 0)
        m = var_misses.get(var, 0)
        marker = "[ok]" if m == 0 else "[x]"
        print(f"  {marker} {var:<10}: {h:>3} hits / {m:>3} misses")
    return 0 if first_div_frame is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
