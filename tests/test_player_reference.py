"""Diff the pure-Python :class:`pysidwizard.player.SWMPlayer` output
against ground-truth SID-register captures from SID-Wizard on
``asid-vice`` (vendored into ``tests/fixtures/`` from
``sidwizard-driver``).

The four reference cases are marked ``xfail(strict=False)`` because
the player still has known gaps (vibrato, portamento, chord tables,
tempo programs, multi-frame hard restart, etc.). When a tune starts
matching exactly it will flip to ``xpassed`` and the implementer
should drop its xfail marker.

In addition to the four end-to-end cases, the file exercises the diff
harness's own machinery (reference loader, frame shift, per-frame
state reconstruction) with synthetic inputs — those tests are real
pass-required tests, not progress meters.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._diff_harness import (
    DiffResult,
    Divergence,
    detect_transition_end,
    diff_against_reference,
    load_reference_csv,
    normalize_reference,
    parse_ignore_regs,
    reconstruct_state_per_frame,
    reg_name,
)
from tests._swm_cache import swm_path as _fetch_swm  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------
# Harness-internals tests (must always pass).
# --------------------------------------------------------------------------


def test_load_reference_csv_round_trips_frame_reg_value(tmp_path):
    csv_path = tmp_path / "ref.csv"
    csv_path.write_text("frame,reg,value\n10,5,170\n11,5,170\n12,6,1\n")
    rows = load_reference_csv(csv_path)
    assert rows == [(10, 5, 170), (11, 5, 170), (12, 6, 1)]


def test_load_reference_csv_rejects_bad_header(tmp_path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("f,r,v\n1,2,3\n")
    with pytest.raises(ValueError, match="unexpected CSV header"):
        load_reference_csv(csv_path)


def test_normalize_reference_shifts_to_zero():
    offset, rows = normalize_reference([(353, 4, 0x41), (360, 0, 0x59)])
    assert offset == 353
    assert rows == [(0, 4, 0x41), (7, 0, 0x59)]


def test_normalize_reference_empty():
    offset, rows = normalize_reference([])
    assert offset == 0
    assert rows == []


def test_reconstruct_state_per_frame_writes_persist_across_silence():
    # Frame 0 writes reg 4 = 0x41. Frame 2 writes reg 4 = 0x40.
    # Frame 1 should still read 0x41; frame 3+ should read 0x40.
    snaps = reconstruct_state_per_frame([(0, 4, 0x41), (2, 4, 0x40)], n_frames=4)
    assert [s[4] for s in snaps] == [0x41, 0x41, 0x40, 0x40]
    # Untouched registers stay at 0.
    assert all(s[7] == 0 for s in snaps)


def test_reconstruct_state_per_frame_multiple_writes_same_frame():
    snaps = reconstruct_state_per_frame([(0, 4, 0x40), (0, 4, 0x41)], n_frames=2)
    # Later write wins within the same frame.
    assert snaps[0][4] == 0x41
    assert snaps[1][4] == 0x41


def test_parse_ignore_regs_accepts_hex_and_dec():
    assert parse_ignore_regs("") == []
    assert parse_ignore_regs("0x15,0x16,21,22") == [0x15, 0x16, 21, 22]


def test_reg_name_covers_known_registers_and_falls_back():
    assert reg_name(0x00) == "v0 FREQ_LO"
    assert reg_name(0x18) == "MODE_VOL"
    assert reg_name(0x99) == "REG_99"


def test_divergence_render_is_human_readable():
    d = Divergence(frame=42, reg=0x01, ref_value=0x10, our_value=0x20)
    out = d.render()
    assert "frame    42" in out
    assert "$D401" in out
    assert "v0 FREQ_HI" in out
    assert "ref=$10" in out
    assert "ours=$20" in out


def test_diff_against_reference_self_match():
    """Diff a freshly generated CSV against the same player run — must
    match perfectly. This is the harness's own integrity test."""
    from pysidwizard.player import iter_writes
    from pysidwizard.reader import read_swm

    swm = read_swm(str(_fetch_swm("flashitback")))
    rows = list(iter_writes(swm, n_frames=120, dedupe=True))
    # Make the synthetic "reference" look like a real capture by shifting
    # all frames forward; the harness should still normalize it back and
    # find zero divergence.
    shifted = [(f + 50, r, v) for (f, r, v) in rows]
    result = diff_against_reference(_fetch_swm("flashitback"), shifted)
    assert isinstance(result, DiffResult)
    assert result.matched, result.render_summary()
    assert result.start_frame_offset == 50
    assert result.first_divergence is None


def test_diff_against_reference_rejects_empty():
    with pytest.raises(ValueError, match="reference is empty"):
        diff_against_reference(_fetch_swm("flashitback"), [])


def test_detect_transition_end_recognises_sid_clear():
    """A frame with >=15 distinct zero-value register writes is the
    F1 SID-clear signature; the function should return that frame + 1."""
    # Frame 0 = music init (handful of writes). Frame 1 = SID clear
    # (zeros to 18 distinct registers). Frame 2 = first music tick.
    music_init = [(0, 4, 0x41), (0, 5, 0x33), (0, 6, 0xAA)]
    sid_clear = [
        (1, r, 0) for r in (0, 1, 2, 3, 4, 7, 8, 9, 10, 11, 14, 15, 16, 17, 18, 21, 22, 23)
    ]
    music = [(2, 0, 0xA1), (2, 1, 0x01)]
    assert detect_transition_end(music_init + sid_clear + music) == 2


def test_detect_transition_end_no_transition_returns_zero():
    """A capture that's already past the F1 transition shouldn't trigger
    auto-skip — return 0 so the harness uses the reference as-is."""
    rows = [(0, 4, 0x41), (0, 0, 0xA1), (1, 0, 0xB2), (2, 4, 0x40)]
    assert detect_transition_end(rows) == 0


def test_detect_transition_end_picks_last_within_window():
    """If two clear-style frames appear within the scan window (as in
    euphoria where frame 0 is pre-F1 garbage and frame 1 is the real
    clear), return the index past the LATER one."""
    clear_a = [(0, r, 0) for r in range(0, 18)]
    clear_b = [(1, r, 0) for r in range(0, 18)]
    music = [(2, 0, 0x59)]
    assert detect_transition_end(clear_a + clear_b + music) == 2


def test_detect_transition_end_falls_back_to_hr_test_bit():
    """When no SID-clear signature is present, detect the song's row-0
    trigger via the first CTRL test-bit emit ($08). The return value
    is the HR-test-bit frame itself — our player's frame 0 emits the
    HR test-bit, so the two align directly."""
    # Pre-song noise on freq registers (no test bits, no SID clear),
    # then HR test-bit on v0 CTRL at frame 21.
    pre_song = [(f, 0, f * 7) for f in range(20)]
    hr_frame = [(21, 0x04, 0x09)]  # v0 CTRL = test + gate
    music = [(22, 0x04, 0x81)]  # v0 CTRL = WF row 0
    assert detect_transition_end(pre_song + hr_frame + music) == 21


def test_detect_transition_end_hr_at_frame_zero():
    """If the HR test-bit appears on frame 0 (e.g. bronkosaurus),
    the song already starts at frame 0 — return 0."""
    rows = [(0, 0x04, 0x09), (1, 0x04, 0x81)]
    assert detect_transition_end(rows) == 0


def test_detect_transition_end_sid_clear_wins_over_hr():
    """SID-clear in the early window beats a later HR test-bit. This
    protects synthetic test inputs where a synthetic F1 clear is the
    intended marker — both signals can be present in a generated
    reference and the structural SID-clear is unambiguous."""
    sid_clear = [(0, r, 0) for r in range(18)]
    # Our player content with an eventual HR test-bit deep inside.
    hr_later = [(50, 0x04, 0x09)]
    rows = sid_clear + hr_later
    assert detect_transition_end(rows) == 1


def test_diff_against_reference_skip_frames_aligns_correctly():
    """Synthesise a reference whose 'frame 0' is a fake F1 transition
    and whose 'frame 1' onward matches the player; with skip_frames=1
    the diff must match."""
    from pysidwizard.player import iter_writes
    from pysidwizard.reader import read_swm

    swm = read_swm(str(_fetch_swm("flashitback")))
    our = list(iter_writes(swm, n_frames=120, dedupe=True))
    fake_transition = [(0, r, 0) for r in range(25)]
    shifted_our = [(f + 1, r, v) for (f, r, v) in our]
    reference = fake_transition + shifted_our
    # Without skip: the fake transition zeroes show up as divergence.
    bad = diff_against_reference(_fetch_swm("flashitback"), reference)
    assert not bad.matched
    # With skip=1: the transition frame is dropped and the rest aligns.
    good = diff_against_reference(_fetch_swm("flashitback"), reference, skip_frames=1)
    assert good.matched, good.render_summary()
    assert good.skipped_transition_frames == 1


def test_diff_against_reference_skip_transition_auto_detects():
    """Same as above but using the auto-detection flag."""
    from pysidwizard.player import iter_writes
    from pysidwizard.reader import read_swm

    swm = read_swm(str(_fetch_swm("flashitback")))
    our = list(iter_writes(swm, n_frames=120, dedupe=True))
    fake_transition = [(0, r, 0) for r in range(25)]
    shifted_our = [(f + 1, r, v) for (f, r, v) in our]
    reference = fake_transition + shifted_our
    result = diff_against_reference(_fetch_swm("flashitback"), reference, skip_transition=True)
    assert result.matched, result.render_summary()
    assert result.skipped_transition_frames == 1


def test_diff_against_reference_skip_frames_overrides_skip_transition():
    """skip_frames should win when both are supplied — verified by
    asking for a skip that auto-detect wouldn't choose, and checking
    skipped_transition_frames reflects the explicit value."""
    from pysidwizard.player import iter_writes
    from pysidwizard.reader import read_swm

    swm = read_swm(str(_fetch_swm("flashitback")))
    our = list(iter_writes(swm, n_frames=120, dedupe=True))
    fake_transition = [(0, r, 0) for r in range(25)]
    shifted_our = [(f + 1, r, v) for (f, r, v) in our]
    reference = fake_transition + shifted_our
    # Auto-detect would pick 1; force 0 instead.
    result = diff_against_reference(
        _fetch_swm("flashitback"),
        reference,
        skip_transition=True,
        skip_frames=0,
    )
    assert result.skipped_transition_frames == 0
    assert not result.matched, "skip_frames=0 should bypass auto-detection"


# --------------------------------------------------------------------------
# Reference reproduction tests: one per vendored tune.
#
# Every frame and every SID register MUST agree with the reference
# captured from real SID-Wizard running on asid-vice. Any divergence
# fails the test. The 4 fixtures collectively exercise the full feature
# set: pitched notes, $7E gate-off note-FX, chord tables, multispeed
# (frame_speed=2), funktempo, BIGFX portamento, vibrato, filter
# walking, instrument inheritance across F1, the WRPITCH carry chain.
#
# Pre-seeding from ``tests/fixtures/{tune}.ghost.csv`` is required for
# bronkosaurus + rain8580 — see [[pysidwizard-ghost-seed]] for the
# editor-residue inheritance that the ghost CSV restores. The four
# committed ghost CSVs were regenerated with sidwizard-driver v0.4.2
# (includes the FLTCTRL / FLTPOSI / CWEPCNT / DETUNER labels needed
# by the seed). Regenerate with ``tools/regen_ghost_fixtures.sh``.
# --------------------------------------------------------------------------


REFERENCE_TUNES = [
    "flashitback",
    "bronkosaurus",
    "euphoria",
    "rain8580",
]


@pytest.mark.parametrize("tune", REFERENCE_TUNES)
def test_reference_progress_meter(tune, capsys):
    ref_path = FIXTURES / f"{tune}.reference.csv"
    swm_path = _fetch_swm(tune)
    ghost_path = FIXTURES / f"{tune}.ghost.csv"
    assert ref_path.exists(), f"missing fixture {ref_path}"
    assert swm_path.exists(), f"missing data {swm_path}"
    assert ghost_path.exists(), f"missing ghost dump {ghost_path}"

    ref = load_reference_csv(ref_path)
    result = diff_against_reference(
        swm_path,
        ref,
        skip_transition=True,
        ghost_path=ghost_path,
    )

    # Always print so ``pytest -s`` shows the result.
    with capsys.disabled():
        print()
        print(f"[{tune}] {result.render_summary()}")

    # Hard match required: every (frame, register) cell must agree.
    assert result.matched, (
        f"{tune}: first divergence at frame "
        f"{result.first_divergence.frame}/{result.n_frames}; "  # type: ignore[union-attr]
        f"{result.divergent_register_writes} cell(s) differ"
    )
