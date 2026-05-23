"""Per-tick conformance harness for pysidwizard's SWMPlayer.

For each (tune, ghost frame K, voice), seed pysidwizard's voice state
from the ground-truth ghost dump at frame K, advance one PLAYER tick,
then compare the predicted state against ghost[K+1]. Each tick is an
independent test case so per-feature state transitions can be scored
in isolation — without alignment drift from earlier frames.

This complements ``tools/diff_ghost.py`` (which free-runs the player
and reports first divergence) and replaces ``tools/tick_predict.py``'s
single-voice / single-tune scope. The output is a JSON scorecard
keyed by (tune, voice, var) with hit / miss counts plus the
first-miss frame for triage.

Cell-diff conflates every player feature into one number. Conformance
isolates: if FREQMODL hits 100% but FREQLO misses, vibrato counter
math is right but the FREQ composition is wrong. That sharpens "which
routine is buggy" without going through full-frame integration.

CLI::

    python3 tools/conformance.py                  # all 4 fixtures
    python3 tools/conformance.py --tune euphoria  # single tune
    python3 tools/conformance.py --report-all     # verbose per-frame
    python3 tools/conformance.py --out scorecard.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pysidwizard.player import NOTE_FREQ_HI, NOTE_FREQ_LO, SWMPlayer  # noqa: E402
from pysidwizard.reader import read_swm  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
TUNES = ("flashitback", "bronkosaurus", "euphoria", "rain8580")

INST_WF_TABLE_POS = 0x10
PER_VOICE_STRIDE = 7
N_VOICES = 3

# Per-voice ZP variables we track. ``base`` is the v0 zero-page address;
# v1 lives at base+7, v2 at base+14 (player.asm's 7-byte stride).
ZP_NAMES: Dict[int, str] = {
    0x10: "FREQLO",
    0x11: "FREQHI",
    0x12: "PWLOGHO",
    0x13: "PWHIGHO",
    0x14: "WFGHOST",
    0x15: "PTNGATE",
    0x16: "PWEEPCNT",
    0x26: "SPDCNT",
    0x29: "WFTPOS",
    0x2A: "PWTPOS",
    0x2B: "ARPSCNT",
    0x3C: "DPITCH",
    0x4F: "SLIDEVIB",
    0x50: "FREQMODL",
    0x51: "FREQMODH",
    0x52: "VIDELCNT",
    0x53: "VIBFREQU",
    0x54: "VIBRACNT",
    0x68: "CURCHORD",
    0x69: "CHORDPOS",
}


def addr(base: int, voice: int) -> int:
    return base + voice * PER_VOICE_STRIDE


def load_ghost(path: Path) -> Dict[int, Dict[int, int]]:
    """Return ``{frame: {addr: value}}`` from a ghost-dump CSV."""
    out: Dict[int, Dict[int, int]] = defaultdict(dict)
    with open(path, newline="") as fp:
        for row in csv.DictReader(fp):
            out[int(row["frame"])][int(row["addr"])] = int(row["value"])
    return dict(out)


def tune_tempo(swm) -> int:
    """Return the effective row period (frames-per-row) for the
    SID-Wizard tempo encoding."""
    tl, tr = swm.subtune_tempos[0] if swm.subtune_tempos else (6, 6)
    if (tl & 0x80) and (tr & 0x80):
        return (tl & 0x7F) or 6
    return (tl & 0x7F) or 6  # funktempo first slot is a reasonable approximation


def _last_wf_pitch(
    player: SWMPlayer,
    instrument: Any,
    wftpos: int,
    chord_pos: int,
    current_chord: int,
) -> Tuple[int, bool]:
    """Recover the (wf_arp_pitch, wf_arp_absolute) that the LAST WF row
    walked into the player would have produced for FREQLO/HI.

    SID-Wizard's WF arp ABSPTCH/RELPTCH overwrite FREQLO/FREQHI ZP each
    frame the WF table walks; NOP / chord / jump rows don't. To
    accurately back-solve vibrato_offset, we need to know what pitch
    the LAST row contributed. ``WFTPOS`` at the ghost frame points at
    the NEXT row to read; ``WFTPOS - 3`` is the row just processed.
    Walk back through JUMP rows (target may chain) to find the most
    recent pitch-writing row.

    Returns ``(0, False)`` when no decodable pitch row is found within
    a small walkback depth — caller will fall back to the +0 base
    assumption.
    """
    table = instrument.wf_table if instrument else b""
    if not table:
        return _chord_fallback(player, chord_pos, current_chord)
    # WFTPOS is absolute (instrument-base offset); subtract $10 for the
    # WF table base. Look at row at WFTPOS-3.
    pos = wftpos - INST_WF_TABLE_POS - 3
    visited: set = set()
    for _ in range(8):  # bounded walkback
        if pos < 0:
            # WFTPOS at start of table — last row was the loop's tail.
            # Try the row just before the implicit terminator at len(table).
            pos = max(0, ((len(table) - 1) // 3) * 3)
            if pos >= len(table):
                return _chord_fallback(player, chord_pos, current_chord)
        if pos in visited:
            # Followed a JUMP back to a row we already checked. The WF
            # table is in a chord-arp loop with no pitch-writing rows;
            # fall through to the chord-byte fallback.
            return _chord_fallback(player, chord_pos, current_chord)
        visited.add(pos)
        if pos + 1 >= len(table):
            return _chord_fallback(player, chord_pos, current_chord)
        col0 = table[pos]
        arp = table[pos + 1]
        if col0 == 0xFE:  # JUMP — target is at byte 1 (= WFTPOS), follow
            jump_target = table[pos + 1]
            pos = jump_target - INST_WF_TABLE_POS - 3
            continue
        if col0 == 0xFF:  # End-of-table — walk back one row
            pos -= 3
            continue
        # arp byte semantics:
        #   $00..$7E  rel-up    ->  pitch = DPITCH + arp_byte
        #   $7F       chord     ->  pitch = DPITCH + chord_byte_just_read
        #   $80       NOP       ->  pitch unchanged; walk back further
        #   $81..$DF  absolute  ->  pitch = arp_byte & $7F
        #   $E0..$FF  rel-down  ->  pitch = DPITCH + (arp_byte - $100) signed
        if arp == 0x80:
            pos -= 3
            continue
        if arp == 0x7F:
            return _chord_fallback(player, chord_pos, current_chord)
        if arp < 0x80:  # $00..$7E
            return arp, False
        if arp < 0xE0:  # $81..$DF absolute
            return arp & 0x7F, True
        # $E0..$FF rel-down (signed)
        return arp - 0x100, False
    return _chord_fallback(player, chord_pos, current_chord)


def _chord_fallback(
    player: SWMPlayer,
    chord_pos: int,
    current_chord: int,
) -> Tuple[int, bool]:
    """Use the last-played chord byte as the WF arp pitch contribution.

    SID-Wizard's chord routine: PLYCHRD reads chord_table[chord_pos],
    plays it as ``pitch = DPITCH + chord_byte``, then increments
    chord_pos. So at ghost capture time chord_table[chord_pos - 1] is
    the byte just played. Used as the fallback when walkback can't
    find a pitch-writing WF row (e.g. flashitback's ARP whose WF
    table is entirely $7F chord rows).
    """
    if not (1 <= current_chord <= len(player._chord_starts)):
        return 0, False
    chord_byte_idx = chord_pos - 1
    ct = player._chord_table
    if not (0 <= chord_byte_idx < len(ct)):
        return 0, False
    chord_byte = ct[chord_byte_idx]
    if chord_byte in (0x7E, 0x7F):
        return 0, False
    return chord_byte, False


def seed_voice(
    player: SWMPlayer,
    voice: int,
    ghost_frame: Dict[int, int],
    tempo: int,
) -> bool:
    """Inject ghost-frame ZP values into ``player.voices[voice]``.

    Returns False if seeding cannot be performed cleanly for this frame
    (e.g. CURINS references an instrument we can't resolve, or
    DPITCH falls outside the freq table). The caller should skip
    scoring such frames.
    """
    v = player.voices[voice]

    curins = ghost_frame.get(addr(0x3E, voice), 0)
    if 1 <= curins <= len(player.swm.instruments):
        v.instrument = player.swm.instruments[curins - 1]
        v.instrument_idx = curins
        v.octave_shift = v.instrument.octave_shift
        # ARPSPED latched from the instrument at STRTSND.
        v.arp_speed_reload = v.instrument.arp_speed
    else:
        # No instrument selected (e.g. voice in idle state). Predictions
        # for vars that require ins are unreliable; bail.
        return False

    # SID ghosts. ``v.pw_lo`` / ``v.pw_hi`` are the PRE-mask ZP values;
    # ``v.sid_pw_lo`` / ``v.sid_pw_hi`` are the SID-visible (masked)
    # outputs from compose. Seed both.
    v.sid_freq_lo = ghost_frame.get(addr(0x10, voice), 0)
    v.sid_freq_hi = ghost_frame.get(addr(0x11, voice), 0)
    v.pw_lo = ghost_frame.get(addr(0x12, voice), 0)
    v.pw_hi = ghost_frame.get(addr(0x13, voice), 0)
    v.sid_pw_lo = v.pw_lo
    v.sid_pw_hi = v.pw_hi & 0x0F
    v.sid_ctrl = ghost_frame.get(addr(0x14, voice), 0)
    v.ptn_gate = ghost_frame.get(addr(0x15, voice), 0xFF)
    v.pw_sweep_count = ghost_frame.get(addr(0x16, voice), 0)

    # SPDCNT mapping. Ghost shows 1..tempo at $1003 entry (post-inc).
    # Our model wraps to 0 at tempo; treat ghost SPDCNT==tempo as our 0.
    sc = ghost_frame.get(addr(0x26, voice), 0)
    v.speed_counter = 0 if sc >= tempo else sc

    # Table positions: absolute byte offsets in instrument data.
    wftpos = ghost_frame.get(addr(0x29, voice), INST_WF_TABLE_POS)
    v.wf_pos = max(0, wftpos - INST_WF_TABLE_POS)
    pw_table_start = INST_WF_TABLE_POS + len(v.instrument.wf_table) + 1
    # PWTPOS is an ABSOLUTE instrument-base offset (see
    # SWMPlayer._tick_pw_table); seed it directly.
    v.pw_pos = ghost_frame.get(addr(0x2A, voice), pw_table_start)

    v.arp_speed_counter = ghost_frame.get(addr(0x2B, voice), 0)

    # DPITCH back-solve: DPITCH = (v.note + octave_shift + transpose) & 0x7F.
    # We can't separate those three contributions from a single byte; pin
    # transpose to ghost TRANSP and put the residual into v.note.
    dpitch = ghost_frame.get(addr(0x3C, voice), 0)
    transp_raw = ghost_frame.get(addr(0x55, voice), 0)
    v.transpose = transp_raw - 0x100 if transp_raw >= 0x80 else transp_raw
    v.note = (dpitch - v.octave_shift - v.transpose) & 0x7F

    # Vibrato / slide block.
    v.slide_vib = ghost_frame.get(addr(0x4F, voice), 0)
    v.vibrato_freqmod_lo = ghost_frame.get(addr(0x50, voice), 0)
    v.vibrato_freqmod_hi = ghost_frame.get(addr(0x51, voice), 0)
    videl = ghost_frame.get(addr(0x52, voice), 0)
    v.vibrato_delay_counter = videl - 0x100 if videl >= 0x80 else videl
    v.vibrato_period = ghost_frame.get(addr(0x53, voice), 0)
    v.vibrato_freq_counter = ghost_frame.get(addr(0x54, voice), 0)

    # Chord state. Store the raw CURCHORD even if it points past the
    # parsed chord table — SID-Wizard accepts out-of-range default_chord
    # values (e.g. euphoria's TOMDRUM has default_chord=$31 with only 15
    # chords parsed; the player just reads CHDPTRLO past the end of the
    # in-memory table). Our chord playback gates on bounds before
    # indexing _chord_starts, so storing the raw value is safe.
    v.current_chord = ghost_frame.get(addr(0x68, voice), 0)
    v.chord_pos = ghost_frame.get(addr(0x69, voice), 0)

    # Decode the LAST WF row's contribution so we can back-solve a tight
    # vibrato_offset. Without this, vibrato_offset absorbs the WF arp
    # delta and FREQLO/FREQHI hit-rate is artificially low for any
    # instrument whose WF table runs non-zero arp values (most of them).
    wf_arp_pitch, wf_arp_absolute = _last_wf_pitch(
        player,
        v.instrument,
        wftpos,
        v.chord_pos,
        v.current_chord,
    )
    v.wf_arp_pitch = wf_arp_pitch
    v.wf_arp_absolute = wf_arp_absolute

    # Base freq = NOTE_FREQ at the play_note our compose would compute.
    if wf_arp_absolute:
        play_note = wf_arp_pitch & 0x7F
    else:
        play_note = v.note + v.octave_shift + v.transpose + wf_arp_pitch
    play_note = max(0, min(len(NOTE_FREQ_LO) - 1, play_note))
    base_word = (NOTE_FREQ_HI[play_note] << 8) | NOTE_FREQ_LO[play_note]
    ghost_word = (v.sid_freq_hi << 8) | v.sid_freq_lo
    v.vibrato_offset = (ghost_word - base_word) & 0xFFFF

    # Mark voice as triggered + clear HR so compose runs the normal path.
    v.triggered = True
    v.hr_timer = 0
    v.pending_row = None
    v.wf_pitch_carry = False
    v.wf_did_pitch_write = False
    return True


def snapshot_voice(player: SWMPlayer, voice: int, tempo: int) -> Dict[int, int]:
    """Read back ZP-equivalent state from our model post-tick."""
    v = player.voices[voice]
    out: Dict[int, int] = {}

    # FREQLO/HI ZP = base[wf_arp_resulting_note] + vibrato_offset
    # (pre-DETUNER, pre-WRPITCH-carry). Mirror compose's branch.
    if v.wf_arp_absolute:
        note = v.wf_arp_pitch & 0x7F
    else:
        note = v.note + v.wf_arp_pitch + v.transpose + v.octave_shift
    note = max(0, min(len(NOTE_FREQ_LO) - 1, note))
    zp_word = ((NOTE_FREQ_HI[note] << 8) | NOTE_FREQ_LO[note]) + v.vibrato_offset
    zp_word &= 0xFFFF
    out[addr(0x10, voice)] = zp_word & 0xFF
    out[addr(0x11, voice)] = (zp_word >> 8) & 0xFF
    out[addr(0x12, voice)] = v.pw_lo & 0xFF
    out[addr(0x13, voice)] = v.pw_hi & 0xFF
    out[addr(0x14, voice)] = v.sid_ctrl & 0xFF
    out[addr(0x15, voice)] = v.ptn_gate & 0xFF
    out[addr(0x16, voice)] = v.pw_sweep_count & 0xFF

    # SPDCNT: ours 0..tempo-1; ghost 1..tempo. Map 0->tempo.
    sc = v.speed_counter if v.speed_counter != 0 else tempo
    out[addr(0x26, voice)] = sc & 0xFF

    if v.instrument is not None:
        out[addr(0x29, voice)] = (INST_WF_TABLE_POS + v.wf_pos) & 0xFF
        # pw_pos is already an absolute instrument-base offset.
        out[addr(0x2A, voice)] = v.pw_pos & 0xFF
    else:
        out[addr(0x29, voice)] = 0
        out[addr(0x2A, voice)] = 0

    out[addr(0x2B, voice)] = v.arp_speed_counter & 0xFF
    out[addr(0x3C, voice)] = (v.note + v.octave_shift + v.transpose) & 0x7F
    out[addr(0x4F, voice)] = v.slide_vib & 0xFF
    out[addr(0x50, voice)] = v.vibrato_freqmod_lo & 0xFF
    out[addr(0x51, voice)] = v.vibrato_freqmod_hi & 0xFF
    out[addr(0x52, voice)] = v.vibrato_delay_counter & 0xFF
    out[addr(0x53, voice)] = v.vibrato_period & 0xFF
    out[addr(0x54, voice)] = v.vibrato_freq_counter & 0xFF
    out[addr(0x68, voice)] = v.current_chord & 0xFF
    out[addr(0x69, voice)] = v.chord_pos & 0xFF
    return out


def conform_tune(
    tune: str,
    swm_path: Path,
    ghost_path: Path,
    start_frame: int,
    max_frames: Optional[int],
    report_all: bool,
) -> Dict[str, Any]:
    """Run the seed-and-predict loop on one tune.

    Returns a dict::

        {voice: {var: {"hits": N, "misses": N,
                       "first_miss_frame": K | None,
                       "first_miss_ref": v, "first_miss_ours": v}}}
    """
    swm = read_swm(str(swm_path))
    tempo = tune_tempo(swm)
    # Ghost-dump halts at $1003 (PLAYER entry), which fires exactly once
    # per video frame regardless of frame_speed. Between ghost[K] and
    # ghost[K+1] the IRQ chain runs ``frame_speed`` ticks (one PLAYER
    # plus frame_speed-1 MULPLY). So we step the GHOST index by 1 and
    # the play_frame call count by ``play_frames_per_ghost``.
    play_frames_per_ghost = max(1, swm.frame_speed)
    ghost = load_ghost(ghost_path)
    n_ghost = max(ghost) + 1 if ghost else 0
    end_frame = n_ghost - 1
    if max_frames is not None:
        end_frame = min(end_frame, start_frame + max_frames)

    per_voice: Dict[int, Dict[str, Dict[str, Any]]] = {
        vi: {
            name: {
                "hits": 0,
                "misses": 0,
                "first_miss_frame": None,
                "first_miss_ref": None,
                "first_miss_ours": None,
            }
            for name in ZP_NAMES.values()
        }
        for vi in range(N_VOICES)
    }
    skipped_frames = 0
    scored_frames = 0

    # Frames where ghost SPDCNT indicates the row-trigger phase
    # (TICK_0/1/2) are skipped: SID-Wizard's HARDRST chain at these
    # ticks gates PW / Filter / Vibrato / WF based on the pattern row
    # just-read into CURNOT/CURIFX, but the harness can't reconstruct
    # pattern position from ghost ZP alone (PTNPOS is a byte offset,
    # not a row index; rows are variable-length). Without the right
    # pending_row our model's gating produces wrong predictions for
    # most per-frame ZP variables. SPDCNT cycle: $tempo = TICK_0,
    # $01 = TICK_1, $02 = TICK_2. For tempo>=4 these are the row-
    # trigger phase; for tempo=3 the entire row is row-trigger so
    # scoring would be useless. Skip these ticks across all voices.
    row_trigger_sc = {tempo, 1, 2} if tempo >= 4 else set(range(tempo + 1))

    for k in range(start_frame, end_frame):
        if k not in ghost or (k + 1) not in ghost:
            continue
        # Skip row-trigger frames where ghost SPDCNT == tempo (TICK_0),
        # 1 (TICK_1), or 2 (TICK_2). Use v0 as the canonical SPDCNT
        # — voices share tempo, so any voice's SPDCNT works.
        sc = ghost[k].get(addr(0x26, 0), 0)
        if sc in row_trigger_sc:
            skipped_frames += 1
            continue
        # Fresh player per seed so previous tick's state doesn't leak.
        player = SWMPlayer(swm)
        any_seeded = False
        seeded_voices: List[int] = []
        for vi in range(N_VOICES):
            if seed_voice(player, vi, ghost[k], tempo):
                seeded_voices.append(vi)
                any_seeded = True
        if not any_seeded:
            skipped_frames += 1
            continue
        # Run one video frame of IRQ ticks: one PLAYER + (frame_speed-1)
        # MULPLY. Ghost captures only at PLAYER entry, so the next ghost
        # frame K+1 corresponds to AFTER all of those have run.
        for _ in range(play_frames_per_ghost):
            player.play_frame()
        ref_next = ghost[k + 1]
        any_scored = False
        for vi in seeded_voices:
            snap = snapshot_voice(player, vi, tempo)
            for base, name in ZP_NAMES.items():
                a = addr(base, vi)
                if a not in ref_next or a not in snap:
                    continue
                ref_val = ref_next[a]
                our_val = snap[a]
                rec = per_voice[vi][name]
                if our_val == ref_val:
                    rec["hits"] += 1
                else:
                    rec["misses"] += 1
                    if rec["first_miss_frame"] is None:
                        rec["first_miss_frame"] = k
                        rec["first_miss_ref"] = ref_val
                        rec["first_miss_ours"] = our_val
                    if report_all:
                        print(
                            f"  {tune} frm{k}->{k+1}  "
                            f"${a:02X} {name}_v{vi}: "
                            f"ref=${ref_val:02X} ours=${our_val:02X}"
                        )
                any_scored = True
        if any_scored:
            scored_frames += 1

    return {
        "tune": tune,
        "tempo": tempo,
        "frame_speed": swm.frame_speed,
        "ghost_frames": n_ghost,
        "scored_frames": scored_frames,
        "skipped_frames": skipped_frames,
        "voices": per_voice,
    }


def print_summary(results: List[Dict[str, Any]]) -> None:
    """Markdown table: rows = vars, cols = (tune, voice). Cell = hit %."""
    var_names = list(ZP_NAMES.values())
    headers = ["var"]
    cols: List[Tuple[str, int]] = []
    for r in results:
        for vi in range(N_VOICES):
            cols.append((r["tune"], vi))
            headers.append(f"{r['tune'][:4]}v{vi}")
    print("\n## Conformance scorecard (hit %)\n")
    print("| " + " | ".join(headers) + " |")
    print("|" + "|".join("---" for _ in headers) + "|")
    for name in var_names:
        row = [name]
        for r in results:
            for vi in range(N_VOICES):
                rec = r["voices"][vi][name]
                total = rec["hits"] + rec["misses"]
                if total == 0:
                    row.append("--")
                else:
                    pct = 100.0 * rec["hits"] / total
                    row.append(f"{pct:5.1f}")
        print("| " + " | ".join(row) + " |")
    print("\n## Totals per tune\n")
    for r in results:
        total_hits = sum(rec["hits"] for vrecs in r["voices"].values() for rec in vrecs.values())
        total_misses = sum(
            rec["misses"] for vrecs in r["voices"].values() for rec in vrecs.values()
        )
        total = total_hits + total_misses
        pct = (100.0 * total_hits / total) if total else 0.0
        print(
            f"- **{r['tune']}** (tempo={r['tempo']}, "
            f"frame_speed={r['frame_speed']}, "
            f"scored={r['scored_frames']}, skipped={r['skipped_frames']}): "
            f"{total_hits}/{total} = {pct:.1f}%"
        )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--tune", choices=TUNES, default=None, help="run a single tune; default is all 4"
    )
    ap.add_argument("--start-frame", type=int, default=1)
    ap.add_argument(
        "--max-frames", type=int, default=None, help="cap on number of ghost frames per tune"
    )
    ap.add_argument(
        "--report-all", action="store_true", help="print every per-frame miss (verbose)"
    )
    ap.add_argument("--out", type=Path, default=None, help="write the full JSON scorecard here")
    args = ap.parse_args(argv)

    tunes = [args.tune] if args.tune else list(TUNES)
    results: List[Dict[str, Any]] = []
    for tune in tunes:
        swm_path = REPO_ROOT / "tests" / "data" / f"{tune}.swm"
        ghost_path = FIXTURES / f"{tune}.ghost.csv"
        if not ghost_path.exists():
            print(f"missing ghost fixture: {ghost_path}", file=sys.stderr)
            return 2
        if not swm_path.exists():
            print(f"missing SWM: {swm_path}", file=sys.stderr)
            return 2
        results.append(
            conform_tune(
                tune,
                swm_path,
                ghost_path,
                args.start_frame,
                args.max_frames,
                args.report_all,
            )
        )

    print_summary(results)

    if args.out:
        args.out.write_text(json.dumps(results, indent=2, default=int))
        print(f"\nwrote scorecard JSON -> {args.out}")

    # Exit non-zero if any var has a miss anywhere (so CI can ratchet).
    for r in results:
        for vrecs in r["voices"].values():
            for rec in vrecs.values():
                if rec["misses"] > 0:
                    return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
