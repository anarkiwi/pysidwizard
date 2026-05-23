"""Test-only: seed ``SWMPlayer`` from a SID-Wizard ghost-register dump.

SID-Wizard's editor leaves selected per-voice zero-page state alive
across F1 song initialization (CURINS, WFGHOST, WFTPOS, FREQLO, ...)
because INITER's INIPVAR doesn't reach the CONST_VAR region.
pysidwizard's cold-start has all of it at zero; for voices whose
row 0 doesn't run STRTSND (= an empty row, a ``$7E`` gate-off
note-FX, ...) that ZP state drives the first dozen frames of SID
writes. The ghost-dump captured by ``sidwizard-driver`` records
those values; this module replays them into a fresh ``SWMPlayer``.

Selective: a voice is seeded only when its row 0 doesn't STRTSND.
STRTSND-on-row-0 voices overwrite their per-voice ZP from scratch,
so seeding them would clobber correct work.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pysidwizard.player import NOTE_FREQ_HI, NOTE_FREQ_LO, SWMPlayer

INST_WF_TABLE_POS = 0x10
PER_VOICE_STRIDE = 7
N_VOICES = 3

# Filter-program self-modifying operand labels, written by
# sidwizard-driver v0.3.0+ ghost_dump. The CSV column for these is
# ``label`` and their addresses live in $1xxx (player code), not ZP.
SELFMOD_FLTCTRL = "FLTCTRL"
SELFMOD_FLTPOSI = "FLTPOSI"
SELFMOD_CWEPCNT = "CWEPCNT"


def _zp_addr(base: int, voice: int) -> int:
    """ZP address for ``base`` on the per-voice 7-byte stride."""
    return base + voice * PER_VOICE_STRIDE


def load_ghost_csv(path: Path) -> Dict[int, Dict[int, int]]:
    """Return ``{frame: {addr: value}}`` from a ghost-dump CSV.

    Format produced by ``sidwizard-driver``'s ``ghost_dump``: header
    ``frame,addr,value[,label]``, one row per (frame, addr) pair.
    Player-code self-modifying operand rows (FLTCTRL/FLTPOSI/CWEPCNT)
    live alongside the ZP rows under the same schema — they're just
    keyed by their $1xxx absolute address. Callers that need them by
    label use :func:`load_ghost_selfmod`.
    """
    out: Dict[int, Dict[int, int]] = defaultdict(dict)
    with open(path, newline="") as fp:
        for row in csv.DictReader(fp):
            out[int(row["frame"])][int(row["addr"])] = int(row["value"])
    return dict(out)


def load_ghost_selfmod(path: Path) -> Dict[int, Dict[str, int]]:
    """Return ``{frame: {label: value}}`` for FLTCTRL / FLTPOSI /
    CWEPCNT rows in a ghost CSV (sidwizard-driver v0.3.0+).

    Requires the ``--annotate`` form of the dump so labels are
    populated; returns an empty dict for an un-annotated CSV.
    """
    out: Dict[int, Dict[str, int]] = defaultdict(dict)
    targets = {SELFMOD_FLTCTRL, SELFMOD_FLTPOSI, SELFMOD_CWEPCNT}
    with open(path, newline="") as fp:
        reader = csv.DictReader(fp)
        if "label" not in (reader.fieldnames or ()):
            return {}
        for row in reader:
            label = row["label"]
            if label in targets:
                out[int(row["frame"])][label] = int(row["value"])
    return dict(out)


def tune_tempo(swm) -> int:
    """Effective row period (frames per row) for the SID-Wizard tempo."""
    tl, _ = swm.subtune_tempos[0] if swm.subtune_tempos else (6, 6)
    return (tl & 0x7F) or 6


def _chord_fallback(
    player: SWMPlayer,
    chord_pos: int,
    current_chord: int,
) -> Tuple[int, bool]:
    """Use the last-played chord byte as the WF arp pitch contribution.

    Used when the WF-row walkback can't find a pitch-writing row
    (e.g. flashitback's ARP whose WF table is entirely $7F chord rows).
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


def _last_wf_pitch(
    player: SWMPlayer,
    instrument: Any,
    wftpos: int,
    chord_pos: int,
    current_chord: int,
) -> Tuple[int, bool]:
    """Recover ``(wf_arp_pitch, wf_arp_absolute)`` for the most recent
    pitch-writing WF row before ``wftpos``. Used to back-solve a tight
    ``vibrato_offset`` so the seeded FREQLO/HI matches what compose
    would produce.
    """
    table = instrument.wf_table if instrument else b""
    if not table:
        return _chord_fallback(player, chord_pos, current_chord)
    pos = wftpos - INST_WF_TABLE_POS - 3
    visited: set = set()
    for _ in range(8):
        if pos < 0:
            pos = max(0, ((len(table) - 1) // 3) * 3)
            if pos >= len(table):
                return _chord_fallback(player, chord_pos, current_chord)
        if pos in visited:
            return _chord_fallback(player, chord_pos, current_chord)
        visited.add(pos)
        if pos + 1 >= len(table):
            return _chord_fallback(player, chord_pos, current_chord)
        col0 = table[pos]
        arp = table[pos + 1]
        if col0 == 0xFE:
            pos = table[pos + 1] - INST_WF_TABLE_POS - 3
            continue
        if col0 == 0xFF:
            pos -= 3
            continue
        if arp == 0x80:
            pos -= 3
            continue
        if arp == 0x7F:
            return _chord_fallback(player, chord_pos, current_chord)
        if arp < 0x80:
            return arp, False
        if arp < 0xE0:
            return arp & 0x7F, True
        return arp - 0x100, False
    return _chord_fallback(player, chord_pos, current_chord)


def _seed_voice(
    player: SWMPlayer,
    voice: int,
    ghost_frame: Dict[int, int],
    tempo: int,
) -> bool:
    """Inject ghost ZP into ``player.voices[voice]``. Returns False if
    seeding can't be performed cleanly (e.g. ghost CURINS references an
    instrument we can't resolve)."""
    v = player.voices[voice]

    curins = ghost_frame.get(_zp_addr(0x3E, voice), 0)
    if 1 <= curins <= len(player.swm.instruments):
        v.instrument = player.swm.instruments[curins - 1]
        v.instrument_idx = curins
        v.octave_shift = v.instrument.octave_shift
    else:
        return False

    v.sid_freq_lo = ghost_frame.get(_zp_addr(0x10, voice), 0)
    v.sid_freq_hi = ghost_frame.get(_zp_addr(0x11, voice), 0)
    v.pw_lo = ghost_frame.get(_zp_addr(0x12, voice), 0)
    v.pw_hi = ghost_frame.get(_zp_addr(0x13, voice), 0)
    v.sid_pw_lo = v.pw_lo
    v.sid_pw_hi = v.pw_hi & 0x0F
    v.sid_ctrl = ghost_frame.get(_zp_addr(0x14, voice), 0)
    v.ptn_gate = ghost_frame.get(_zp_addr(0x15, voice), 0xFF)
    v.pw_sweep_count = ghost_frame.get(_zp_addr(0x16, voice), 0)

    sc = ghost_frame.get(_zp_addr(0x26, voice), 0)
    v.speed_counter = 0 if sc >= tempo else sc

    wftpos = ghost_frame.get(_zp_addr(0x29, voice), INST_WF_TABLE_POS)
    v.wf_pos = max(0, wftpos - INST_WF_TABLE_POS)
    pw_table_start = INST_WF_TABLE_POS + len(v.instrument.wf_table) + 1
    pwtpos = ghost_frame.get(_zp_addr(0x2A, voice), pw_table_start)
    v.pw_pos = max(0, pwtpos - pw_table_start)

    v.arp_speed_counter = ghost_frame.get(_zp_addr(0x2B, voice), 0)

    dpitch = ghost_frame.get(_zp_addr(0x3C, voice), 0)
    transp_raw = ghost_frame.get(_zp_addr(0x55, voice), 0)
    v.transpose = transp_raw - 0x100 if transp_raw >= 0x80 else transp_raw
    # Back-solve v.note so compose's ``v.note + octave_shift + transpose``
    # reproduces ghost DPITCH. Keep the result SIGNED (don't ``& 0x7F``):
    # SID-Wizard's CLEGATO does an unmasked ADC chain and the WF arp's
    # subsequent ``adc DPITCH`` is an 8-bit modular operation. If we mask
    # here, an underflow like ``dpitch=1 - transpose=3`` lands at 126
    # ($7E) instead of -2, and the later arp addition reads the wrong
    # FREQTBL row (132 clamped to 95 -> $F810 instead of 4 -> $014B on
    # rain8580 v0 frame 5).
    v.note = dpitch - v.octave_shift - v.transpose

    v.slide_vib = ghost_frame.get(_zp_addr(0x4F, voice), 0)
    v.vibrato_freqmod_lo = ghost_frame.get(_zp_addr(0x50, voice), 0)
    v.vibrato_freqmod_hi = ghost_frame.get(_zp_addr(0x51, voice), 0)
    videl = ghost_frame.get(_zp_addr(0x52, voice), 0)
    v.vibrato_delay_counter = videl - 0x100 if videl >= 0x80 else videl
    v.vibrato_period = ghost_frame.get(_zp_addr(0x53, voice), 0)
    v.vibrato_freq_counter = ghost_frame.get(_zp_addr(0x54, voice), 0)

    v.current_chord = ghost_frame.get(_zp_addr(0x68, voice), 0)
    v.chord_pos = ghost_frame.get(_zp_addr(0x69, voice), 0)

    wf_arp_pitch, wf_arp_absolute = _last_wf_pitch(
        player,
        v.instrument,
        wftpos,
        v.chord_pos,
        v.current_chord,
    )
    v.wf_arp_pitch = wf_arp_pitch
    v.wf_arp_absolute = wf_arp_absolute

    if wf_arp_absolute:
        play_note = wf_arp_pitch & 0x7F
    else:
        play_note = v.note + v.octave_shift + v.transpose + wf_arp_pitch
    play_note = max(0, min(len(NOTE_FREQ_LO) - 1, play_note))
    base_word = (NOTE_FREQ_HI[play_note] << 8) | NOTE_FREQ_LO[play_note]
    ghost_word = (v.sid_freq_hi << 8) | v.sid_freq_lo
    v.vibrato_offset = (ghost_word - base_word) & 0xFFFF

    v.triggered = True
    v.hr_timer = 0
    v.wf_pitch_carry = False
    v.wf_did_pitch_write = False
    return True


def _row_triggers_strtsnd(row) -> bool:
    """True if ``row`` has a real pitched note that would run STRTSND.

    STRTSND fires for notes in ``1..0x5F`` (= musical pitches), but NOT
    for note-FX ($60..$7E), zero/empty rows, or rows with portamento
    BIGFX (which take the NONEWNO branch).
    """
    if row is None:
        return False
    if row.note is None or row.note == 0:
        return False
    if row.note >= 0x60:
        return False
    if row.fx == 0x03:
        return False
    return True


def _seed_filter_globals(
    player: SWMPlayer,
    selfmod_frame0: Dict[str, int],
    seed_sid_state: Optional[List[int]],
) -> None:
    """Inherit the editor's filter-program state across F1.

    SID-Wizard's INIPVAR doesn't touch the self-modifying FLTCTRL /
    FLTPOSI / CWEPCNT operand bytes nor the CTFH_GHO / CTFL_GHO sweep
    accumulators; the editor's last filter walk persists into the
    song. We restore:

    - ``player.filter_controller_voice`` from ghost FLTCTRL (= X
      register voice index × 7).
    - Per-voice ``v.filt_pos`` (= FLTPOSI - instrument's filter-table
      offset) on the controller voice.
    - Per-voice ``v.filt_sweep_count`` (= CWEPCNT) on the controller.
    - ``player.filter_cutoff_hi`` / ``filter_cutoff_lo`` from the
      reference SID state ($D416 / $D415). Ghost doesn't carry the
      cutoff ghosts directly, but they equal the SID-register state
      at the seed frame.
    """
    if seed_sid_state is not None:
        player.filter_cutoff_lo = seed_sid_state[0x15]
        player.filter_cutoff_hi = seed_sid_state[0x16]
        # $D417 high nibble = resonance; low nibble = routing (recomposed
        # per-frame from voice_in_filter). $D418 high nibble = mode bits
        # (LP/BP/HP), low nibble = master volume.
        player.filter_resonance = seed_sid_state[0x17] & 0xF0
        player.filter_mode_vol = seed_sid_state[0x18]

    if not selfmod_frame0:
        return
    fltctrl = selfmod_frame0.get(SELFMOD_FLTCTRL)
    fltposi = selfmod_frame0.get(SELFMOD_FLTPOSI)
    cwepcnt = selfmod_frame0.get(SELFMOD_CWEPCNT, 0)
    if fltctrl is None or fltposi is None:
        return
    voice_idx = fltctrl // PER_VOICE_STRIDE
    if not (0 <= voice_idx < N_VOICES):
        return
    controller = player.voices[voice_idx]
    player.filter_controller_voice = controller
    ins = controller.instrument
    if ins is None:
        return
    filt_table_start = INST_WF_TABLE_POS + len(ins.wf_table) + 1 + len(ins.pw_table) + 1
    rel_pos = fltposi - filt_table_start
    if 0 <= rel_pos <= len(ins.filter_table):
        controller.filt_pos = rel_pos
        controller.filt_sweep_count = cwepcnt


def seed_player_for_cell_diff(
    player: SWMPlayer,
    ghost_frame0: Dict[int, int],
    seed_sid_state: Optional[List[int]] = None,
    selfmod_frame0: Optional[Dict[str, int]] = None,
) -> List[int]:
    """Seed ``player`` voices selectively from the ghost frame-0 ZP
    dump. Only voices whose row 0 doesn't STRTSND get seeded — the
    others would lose their cold-start row-trigger logic.

    ``seed_sid_state`` (length 25) is the reference SID register state
    at the end of the last dropped F1-transition frame. When provided,
    we seed ``v.sid_ad`` / ``v.sid_sr`` from it for seeded voices
    (ghost CSVs don't carry AD/SR), and seed the global filter-cutoff
    + resonance + mode/volume from $D415..$D418.

    ``selfmod_frame0`` is the FLTCTRL/FLTPOSI/CWEPCNT row dict from
    :func:`load_ghost_selfmod` at frame 0 (sidwizard-driver v0.3.0+).
    When provided, seeds ``filter_controller_voice`` + the controller's
    per-voice ``filt_pos`` / ``filt_sweep_count``.

    Also derives ``v.voice_in_filter`` for each seeded voice from its
    inherited instrument's filter table (= the same rule
    :meth:`SWMPlayer._start_note` applies).

    Returns the list of voice indices that were seeded.
    """
    seeded: List[int] = []
    for vi in range(N_VOICES):
        v = player.voices[vi]
        if _row_triggers_strtsnd(v.pending_row):
            continue
        # Preserve pending_row + speed_counter so the player still runs
        # the row's gate-off / inst-fx / etc. on top of the seeded state.
        saved_pending = v.pending_row
        saved_sc = v.speed_counter
        if not _seed_voice(player, vi, ghost_frame0, tune_tempo(player.swm)):
            continue
        v.pending_row = saved_pending
        v.speed_counter = saved_sc
        if seed_sid_state is not None:
            base = vi * PER_VOICE_STRIDE
            v.sid_ad = seed_sid_state[base + 5]
            v.sid_sr = seed_sid_state[base + 6]
        # voice_in_filter: same rule as _start_note's SETFLTP block.
        # Ghost FSWITCH isn't directly captured but the inherited
        # instrument's filter table tells us the routing bit.
        ft = v.instrument.filter_table if v.instrument else b""
        v.voice_in_filter = bool(ft) and ft[0] != 0xFF
        seeded.append(vi)
    _seed_filter_globals(player, selfmod_frame0 or {}, seed_sid_state)
    return seeded
