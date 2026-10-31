"""The filter controller: SETFLTP's claim, FILTPRG's one global CWEPCNT and FLTPOSI walk.

Each test steps a SWMPlayer and the 6502 of the same SID-Maker export together, asserting
every call's SID writes and memory agree, and reads FLTCTRL / FLTPOSI / CWEPCNT (operands
of the player's own code) at the binder's addresses.
"""

from __future__ import annotations

from functools import cached_property
from typing import Optional

import numpy as np
import pytest

from pysidwizard import (
    End,
    Instrument,
    Pattern,
    PlayPattern,
    Row,
    SWMFile,
    SWMPlayer,
    Waveform,
    playspec,
    straight_tempo,
)
from tests._toy import SETUP_FRAMES, STACK, emu

VOICES = tuple(v * playspec.VOICE_STRIDE for v in range(playspec.CHANNELS))
OUTSIDE_STACK = np.ones(0x10000, bool)
OUTSIDE_STACK[STACK.start : STACK.stop] = False


class _Lockstep:
    """A SWMPlayer and the 6502 of its image, stepped together and compared each call."""

    def __init__(self, swm: SWMFile):
        self.spec = SWMPlayer(swm)
        self.cpu = emu(self.spec)
        self.calls = 0

    def step(self, calls: int = 1) -> _Lockstep:
        """Run ``calls`` play calls on both, asserting equal SID writes and memory."""
        for _ in range(calls):
            assert self.spec.play_frame() == self.cpu.play_frame(), f"call {self.calls} writes"
            mine = np.frombuffer(self.spec.memory, np.uint8)
            real = np.frombuffer(bytes(self.cpu._mem), np.uint8)  # pylint: disable=protected-access
            diff = np.flatnonzero((mine != real) & OUTSIDE_STACK)
            assert not diff.size, f"call {self.calls} memory differs at {diff[:8]}"
            self.calls += 1
        return self

    @cached_property
    def lab(self) -> dict:
        """The binder's address map (available once a call has run)."""
        return playspec.labels(self.spec.view(), self.spec.entry)

    def __getitem__(self, name: str) -> int:
        return self.spec.memory[self.lab[name]]

    def voice(self, name: str, voice: int) -> int:
        """Per-voice variable ``name`` of ``voice``."""
        return self.spec.memory[self.lab[name] + voice * playspec.VOICE_STRIDE]

    def poke(self, addr: int, *values: int) -> None:
        """Store ``values`` from ``addr`` in both memories."""
        for k, value in enumerate(values):
            for mem in (self.spec._mem, self.cpu._mem):  # pylint: disable=protected-access
                mem[addr + k] = value

    def set(self, name: str, value: int) -> None:
        """Store ``value`` at label ``name`` in both memories."""
        self.poke(self.lab[name], value)

    def instrument(self, number: int) -> int:
        """The address of instrument ``number`` (1-based) in the image."""
        mem = self.spec.memory
        return mem[self.lab["p_insl1"] + number] | mem[self.lab["p_insh1"] + number] << 8


def _instrument(filter_table: bytes, control: int = 0x00) -> Instrument:
    return Instrument(
        control=control,
        sustain=0xF,
        first_waveform=Waveform.PULSE | 0x01,
        wf_table=bytes([0x41, 0x80, 0xFF]),
        pw_table=b"\x88\x00\x00",
        filter_table=filter_table,
    )


def _filt_start(ins: Instrument) -> int:
    return 0x10 + len(ins.wf_table) + 1 + len(ins.pw_table) + 1


def _swm(first: Instrument, second: Instrument, v1_row: Optional[Row] = None, v0_row=None):
    """v0 plays instrument 1 on row 0 then ``v0_row``; v1 plays ``v1_row`` on row 1; tempo 6."""
    pat_v0 = Pattern(rows=[Row(note=49, instrument=1), v0_row or Row()] + [Row()] * 6, length=8)
    pat_v1 = Pattern(rows=[Row(), v1_row or Row()] + [Row()] * 6, length=8)
    return SWMFile(
        sequences=[[PlayPattern(1), End()], [PlayPattern(2), End()], [PlayPattern(3), End()]],
        patterns=[pat_v0, pat_v1, Pattern(rows=[Row()] * 8, length=8)],
        instruments=[first, second],
        subtune_tempos=[(straight_tempo(6), straight_tempo(6))],
    )


def _swap_swm() -> SWMFile:
    """v0 then (row 1) v1 trigger a single-sweep-row filter instrument: a mid-song swap."""
    up, down = _instrument(bytes([0x10, 0x05, 0x00])), _instrument(bytes([0x10, 0xFA, 0x00]))
    return _swm(up, down, Row(note=49, instrument=2))


def test_sweep_starting_filter_table_claims_controller():
    """SETFLTP claims the filter for any first row but $00/$FF, a sweep row ($01..$7F) too."""
    run = _Lockstep(_swap_swm()).step(SETUP_FRAMES)
    assert run["fltctrl"] not in VOICES
    assert run.step()["fltctrl"] == VOICES[0]


def test_controller_swaps_to_later_voice_mid_song():
    """A later voice's sweep-starting filter instrument takes the filter over."""
    run = _Lockstep(_swap_swm()).step(SETUP_FRAMES + 7)
    assert run["fltctrl"] == VOICES[1]


def test_global_cwepcnt_persists_across_controller_swap():
    """CWEPCNT is one global: the swapped-in controller continues the previous count."""
    run = _Lockstep(_swap_swm()).step(SETUP_FRAMES + 6)
    pre_swap = run["cwepcnt"]
    assert pre_swap == 5
    run.step()
    assert (run["fltctrl"], run["cwepcnt"]) == (VOICES[1], pre_swap)
    assert run.step()["cwepcnt"] == pre_swap + 1


def test_a_zero_first_filter_byte_leaves_fltposi_with_the_controller():
    """SETFLTP's ``beq SWONFL`` routes the voice without touching FLTCTRL / FLTPOSI."""
    first = _instrument(bytes([0x90, 0x10, 0x00, 0x90, 0x20, 0x00]))
    second = _instrument(bytes([0x00, 0x00, 0x00]))
    run = _Lockstep(_swm(first, second, v0_row=Row(note=50, instrument=2)))
    run.step(SETUP_FRAMES + 7)
    assert (run["fltctrl"], run.voice("curins", 0)) == (VOICES[0], 2)
    assert run["fswitch"] & 1
    assert run["fltposi"] > _filt_start(first)


def test_the_filter_reset_switch_skips_setfltp():
    """FILTRESETSW's ``bmi ENDFILRESET`` leaves FLTCTRL untouched on a note without instrument."""
    ins = _instrument(bytes([0x90, 0x10, 0x00]), control=0x80)
    run = _Lockstep(_swm(ins, ins, v0_row=Row(note=50))).step(SETUP_FRAMES + 1)
    assert run["fltctrl"] == VOICES[0]
    run.set("fltctrl", 0x0F)
    note = run.voice("curnot", 0)
    run.step(6)
    assert run.voice("curnot", 0) == note + 1
    assert run["fltctrl"] == 0x0F


def test_the_filter_walk_reads_past_the_instrument():
    """FLTPOSI indexes the instrument pointer like any table cursor, past its tables too."""
    ins = _instrument(bytes([0x00, 0x00, 0x00]))
    pos = 0x45
    run = _Lockstep(_swm(ins, ins)).step(SETUP_FRAMES + 1)
    run.poke(run.instrument(1) + pos, 0xC1, 0x22, 0x00)
    run.set("fltctrl", VOICES[0])
    run.set("fltposi", pos)
    run.step()
    assert (run["ctfhgho"], run["resonib"], run["fltposi"]) == (0x22, 0x10, pos + 3)


@pytest.mark.parametrize("version", ["1.7", "1.97"])
def test_an_ff_first_filter_table_keeps_fltctrl_on_the_track(version):
    """SETFLTP's ``beq STOPFLT`` never clears FLTCTRL; STOPFLT points FLTPOSI at the new $FF."""
    first = _instrument(bytes([0x90, 0x10, 0x00, 0x90, 0x20, 0x00]))
    second = _instrument(b"")
    swm = _swm(first, second, v0_row=Row(note=50, instrument=2))
    swm.player_version = version
    run = _Lockstep(swm).step(SETUP_FRAMES + 7)
    assert run["fltctrl"] == VOICES[0]
    assert not run["fswitch"] & 1
    assert run["fltposi"] == _filt_start(second)


def test_a_swapped_in_sweep_starts_from_the_global_cutoff():
    """FISWEEP adds to the one CTFH_GHO / CTFL_GHO pair, whoever set it."""
    first = _instrument(bytes([0x90, 0x30, 0x00]))
    second = _instrument(bytes([0x10, 0x08, 0x00]))
    run = _Lockstep(_swm(first, second, Row(note=49, instrument=2))).step(SETUP_FRAMES + 7)
    assert (run["fltctrl"], run["ctfhgho"]) == (VOICES[1], 0x30)
    run.step()
    assert (run["ctfhgho"], run["ctflgho"]) == (0x31, 0)


def test_sweep_row_advances_only_when_the_counter_equals_its_cycle_count():
    """FILTPRG's ``cmp CWEPCNT; beq`` is an equality test and ``inc CWEPCNT`` wraps at 256."""
    run = _Lockstep(_swap_swm()).step(SETUP_FRAMES + 1)
    start = run["fltposi"]
    run.set("cwepcnt", 0x20)
    assert (run.step()["fltposi"], run["cwepcnt"]) == (start, 0x21)
    run.set("cwepcnt", 0xFF)
    assert (run.step()["fltposi"], run["cwepcnt"]) == (start, 0x00)


@pytest.mark.parametrize("version, resets", [("1.7", False), ("1.8", True), ("1.97", True)])
def test_jump_to_filter_row_effect(version, resets):
    """BIGFX0B stores FLTPOSI = filter table + 3 * value; from 1.8 it also zeroes CWEPCNT,
    before that the sweep counter keeps its count."""
    swm = _swap_swm()
    swm.player_version = version
    ins = swm.instruments[0]
    ins.filter_table = bytes([0x10, 0x05, 0x00, 0x10, 0x05, 0x00])
    swm.patterns[0].rows[1] = Row(fx=0x0B, fx_value=1)
    run = _Lockstep(swm).step(SETUP_FRAMES + 6)
    count = run["cwepcnt"]
    assert (run["fltposi"], count) == (_filt_start(ins), 5)
    run.step()
    assert (run["fltposi"], run["cwepcnt"]) == (_filt_start(ins) + 3, 0 if resets else count)


def test_jump_to_an_ff_row_is_taken_as_a_set_row():
    """FLTJUMP applies any non-sweep target as a set row without the end test, so a jump onto
    the table's $FF end sets the next byte's cutoff, resonance $F and every band bit."""
    swm = _swap_swm()
    ins = swm.instruments[0]
    start = _filt_start(ins)
    ins.filter_table = bytes([0x90, 0x20, 0x00, 0xFE, start + 6, 0x00])
    run = _Lockstep(swm).step(SETUP_FRAMES + 1)
    end = run.instrument(1) + start + 6
    assert run.spec.memory[end] == 0xFF
    run.set("fltposi", start + 3)
    run.step()
    cutoff = run.spec.memory[end + 1]
    assert (run["ctfhgho"], run["resonib"], run["fltband"]) == (cutoff, 0xF0, 0x70)
    assert run["fltposi"] == start + 9


def test_a_filter_row_jumping_to_itself_changes_nothing():
    """FLTJUMP compares the target with FLTPOSI first (``cmp FLTPOSI ; beq``): a self-jump rests."""
    swm = _swap_swm()
    ins = swm.instruments[0]
    start = _filt_start(ins)
    ins.filter_table = bytes([0x90, 0x20, 0x00, 0xFE, start + 3, 0x00])
    run = _Lockstep(swm).step(SETUP_FRAMES + 1)
    run.set("fltposi", start + 3)
    names = ("fltposi", "ctfhgho", "resonib", "fltband", "cwepcnt")
    before = [run[n] for n in names]
    run.step()
    assert [run[n] for n in names] == before
