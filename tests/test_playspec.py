"""The SID-Wizard playspec against the tune's own 6502: routine proofs and whole songs."""

import json
import random
import sys
from pathlib import Path

import pytest
import z3
from jennings.devices.mpu6502 import MPU
from pysidtracker import SidImage
from pysidtracker.disasm import image_after_init
from pysidtracker.maincli import main as cli
from pysidtracker.playspec import elaborate, run
from pysidtracker.playspec.tv import replay, validate
from pysidtracker.testing import make_tune_fixtures, resolve_tune, song_frames

from pysidwizard import playspec
from tests._swm_cache import SW197_EXPORT_SHA256, sw197_export_path
from tests.test_oracle import TUNES

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import sw_spec_diff  # noqa: E402  (after sys.path tweak)

EXPORTS = ("bronkosaurus", "euphoria", "flashitback", "glacialblues")
_SID_EXPORTS = "native/examples/sid-exports/"
ALL_EXPORTS = tuple(Path(m).stem for m in SW197_EXPORT_SHA256 if m.startswith(_SID_EXPORTS))
STACK = range(0x100, 0x200)
BUDGET = 20000
RANDOM_CASES = 300
# Players the binder refuses, by the reason it gives.
REFUSED_EXPORTS = {
    "echo-test": "echo builds",
    **{name: "multi-SID" for name in ALL_EXPORTS if name.endswith("SID")},
}
REFUSED_TUNES = {
    **{
        name: "partly relocated"
        for name in (
            "moofistication",
            "easyriff",
            "mega_run",
            "mid_of_ages",
            "memory_invaders",
            "club64",
            "too_long_on_disk",
        )
    },
    "sheepoid_dx": "SID-Wizard 1.0",
    "pantheon": "SID-Wizard 1.0",
}
STATED_TUNES = {k: v for k, v in TUNES.items() if k not in REFUSED_TUNES}

tune_id, tune_path = make_tune_fixtures(STATED_TUNES)


def _export(name: str) -> bytes:
    return sw197_export_path(f"{_SID_EXPORTS}{name}.sid").read_bytes()


def _view(data: bytes) -> bytes:
    """The 64 KiB memory after the tune's init."""
    return bytes(image_after_init(SidImage.from_sid(data)).window(0, 0x10000))


def _rows(mem: bytearray, found: dict, rng: random.Random) -> None:
    """A random pattern at a random pointer and random row-reader state."""
    base = rng.choice([0x4000, 0x40C0, 0x7000])
    for field in ("zp_note", "zp_inst", "zp_fx", "zp_value"):
        mem[found[field]], mem[found[field] + 1] = base & 0xFF, base >> 8
    row_bytes = [0, 0, 1, 0x31, 0x70, 0x71, 0x77, 0x78, 0x80, 0x85, 0x90, 0xB1, 0xC0, 0xFF]
    for offset in range(0x100):
        mem[base + offset] = rng.choice(row_bytes + [rng.randrange(256)])
    mem[found["ptnpos"]] = rng.randrange(256)
    mem[found["packcnt"]] = rng.choice([0, 0, 0, 1, 2, 5, 9])
    for cell in ("curnot", "curifx", "curifx_long", "curfx2", "curval"):
        mem[found[cell]] = rng.randrange(256)


def _ticks(mem: bytearray, found: dict, rng: random.Random) -> None:
    """A random tempo program and tick-clock state."""
    for offset in range(0x100):
        mem[found["tempotbl"] + offset] = rng.choice([0, 1, 2, 3, 5, 0x7F, 0x80, 0x81, 0xFF])
    mem[found["spdcnt"]] = rng.choice([0, 1, 2, 3, rng.randrange(256)])
    mem[found["tmppos"]] = rng.randrange(256)
    mem[found["tmpbase"]] = rng.randrange(256)


# Stretches whose code runs on into code they do not state: first address, end, random state.
STRETCHES = {
    "read_row": (lambda b: b["read_row"], lambda b: b["reader_end"], _rows),
    "tick": (lambda b: b["dotrack"], lambda b: b["tick_0"] - 7, _ticks),
}


def _holds(found: dict, column: str, value: int, high: int = 7, low: int = 0):
    """An assumption that bits ``high..low`` of voice 0's ``column`` hold ``value``."""
    return lambda mem0, _regs: z3.Extract(high, low, z3.Select(mem0, found[column])) == value


def _word(view: bytes, at: int) -> int:
    return view[at] | view[at + 1] << 8


def routines(found: dict, view: bytes):
    """``(label, routine, pc, stop, assumptions)`` of each routine of the spec that is a
    routine of the player: from its label, inputs in registers or, for a dispatched
    effect, the effect its column names; a stretch stops where a dispatch takes over."""
    out = [
        ("CNTPLAY", "cntplay", found["cntplay"], None, ()),
        ("CNTPLY2", "cntply2", found["cntply2"], None, ()),
        ("FILTPRG", "filtprg_chain", found["filtprg"], None, ()),
        ("SETPWID", "setpwid_chain", found["setpwid"], None, ()),
        ("WFARPTB", "at_wfarptb", found["wfarptb"], None, ()),
        ("HARDRST", "at_hardrst", found["hardrst"], None, ()),
        ("SETVIB0", "setvib0", found["setvib0"], None, ()),
        ("SETVIB1", "setvib1", found["setvib1"], None, ()),
        ("SETVIBR", "at_setvibr", found["setvibr"], None, ()),
        ("SETFMOD", "at_setfmod", found["setfmod"], None, ()),
        ("FORCVIB", "forcvib", found["forcvib"], None, ()),
        ("STRTSND", "startsound", found["to_strtsnd"], found["setadsr_fx"], ()),
        ("SELNTFX", "at_selntfx", found["selntfx"], found["indexj1_end"] - 2, ()),
        ("SMALPFX", "at_smalsel", found["smalpfx"], found["indexj2_end"] - 2, ()),
        ("BIGPTFX", "at_bigptfx", found["bigptfx"], found["indexjp"] - 1, ()),
    ]
    if found["mulcntp"]:
        out.append(("MULCNTP", "at_mulcntp", found["mulcntp"], None, ()))
    for label, routine, flag in (
        ("SEQ_FX", "at_seq_fx", "seq_fx_support"),
        ("SETSEQA", "at_setseqa", "subtunejump"),
        ("TRAKTMP", "at_traktmp", "tempoprgsupp"),
    ):
        if found[flag]:
            out.append((label, routine, found[label.lower()], None, ()))
    for k in range(8):
        pc = found["indexj1_end"] + view[found["notefxtbl"] + 0x78 + k]
        assume = (_holds(found, "curnot", 0x78 + k),)
        if pc != found["insptfx"]:
            out.append((f"note ${0x78 + k:02x}", "at_note_effect", pc, found["insptfx"], assume))
    for n in range(2, 0x10):
        pc = found["indexj2_end"] + view[found["smallfxtbl"] + n]
        assume = (_holds(found, "curfx2", n, 7, 4),)
        out.append((f"small ${n:x}x", "at_small_effect", pc, None, assume))
    for n in range(1, 0x20):
        pc = _word(view, found["bigfxtable"] + 2 * n)
        out.append((f"big ${n:02x}", "at_big_effect", pc, None, (_holds(found, "curfx2", n),)))
    return out


def _prove(view: bytes, found: dict, routine: str, pc: int, stop=None, assume=()):
    return validate(
        playspec.SPEC,
        found,
        f"voice[0].{routine}",
        view,
        pc,
        registers={"X": 0},
        preserve=("X",),
        stop=stop,
        assume=assume,
    )


def _stretch_replay(found: dict, stretch: str, memory: bytes, entry: int, stop: int) -> list:
    """The addresses outside the stack page where the spec routine and the jennings run
    of the code from ``entry`` to ``stop`` leave ``memory`` differently."""
    spec_mem = run(
        elaborate(playspec.SPEC, found, routine=f"voice[0].{stretch}"),
        bytearray(memory),
        registers={"x": 0},
    )
    cpu_mem = bytearray(memory)
    cpu = MPU(memory=cpu_mem)
    cpu.x, cpu.sp, cpu.pc = 0, 0xFF, entry
    for _ in range(BUDGET):
        if cpu.pc == stop:
            break
        cpu.step()
    else:
        raise AssertionError(f"the code does not reach ${stop:04x}")
    return [a for a in range(0x10000) if spec_mem[a] != cpu_mem[a] and a not in STACK]


def _assert_proven(result, found: dict, label: str, routine: str, pc: int) -> None:
    """A proof, or the counterexample confirmed on jennings."""
    if result.proven:
        return
    cex = result.counterexample
    regs = {k: v for k, v in cex["registers"].items() if k != "SP"}
    diff = replay(playspec.SPEC, found, f"voice[0].{routine}", cex["memory"], pc, regs)
    shown = {k: v for k, v in cex.items() if k not in ("memory", "cells")}
    raise AssertionError(f"{label} at ${pc:04x}: {shown}; replayed differences {diff[:8]}")


def _prove_all(view: bytes) -> None:
    found = playspec.binding(view)
    for label, routine, pc, stop, assume in routines(found, view):
        result = _prove(view, found, routine, pc, stop, assume)
        _assert_proven(result, found, label, routine, pc)


@pytest.mark.parametrize("name", EXPORTS)
def test_the_voices_cells_sit_seven_bytes_apart_from_the_found_addresses(name):
    found = playspec.binding(_view(_export(name)))
    program = elaborate(playspec.SPEC, found)
    for cell_name in ("curval", "spdcnt", "transp"):
        placed = sorted(
            a for cell in program.cells if cell.name == cell_name for a in cell.extent()
        )
        assert placed == [found[cell_name] + 7 * voice for voice in range(3)]


@pytest.mark.parametrize("stretch", STRETCHES)
@pytest.mark.parametrize("name", EXPORTS)
def test_stretch_equals_the_6502_on_random_state(name, stretch):
    """Every random state leaves memory as the 6502 code leaves it."""
    view = _view(_export(name))
    found = playspec.binding(view)
    entry, stop = STRETCHES[stretch][0](found), STRETCHES[stretch][1](found)
    rng = random.Random(name)
    for _ in range(RANDOM_CASES):
        memory = bytearray(view)
        STRETCHES[stretch][2](memory, found, rng)
        assert not _stretch_replay(found, stretch, bytes(memory), entry, stop)


@pytest.mark.parametrize("stretch", STRETCHES)
@pytest.mark.parametrize("name", EXPORTS)
def test_stretch_is_proven_against_the_6502(name, stretch):
    """The spec routine equals the 6502 code up to ``stop`` for all initial memory."""
    view = _view(_export(name))
    found = playspec.binding(view)
    pc = STRETCHES[stretch][0](found)
    result = _prove(view, found, stretch, pc, STRETCHES[stretch][1](found))
    _assert_proven(result, found, stretch, stretch, pc)


@pytest.mark.parametrize("name", EXPORTS)
def test_every_routine_is_proven_against_the_6502(name):
    """Each routine the spec states equals the player's code from its label for all
    initial memory: the same bytes written outside the stack page, X kept."""
    _prove_all(_view(_export(name)))


@pytest.mark.parametrize("name", ALL_EXPORTS)
def test_spec_memory_equals_the_6502_over_the_whole_song(name):
    """SpecPlayer over the spec and the tune's own play routine leave the SID registers
    and the 64 KiB outside the stack page equal after every call of the song."""
    data = _export(name)
    if name in REFUSED_EXPORTS:
        with pytest.raises(ValueError, match=REFUSED_EXPORTS[name]):
            playspec.binding(_view(data))
        return
    assert sw_spec_diff.first_difference(data, song_frames(data, 1), progress=False) is None


def test_the_cli_checks_the_spec_with_a_bound_layout(tmp_path, capsys):
    """``pysidtracker playspec check`` accepts the spec bound to a tune's layout."""
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps(playspec.binding(_view(_export("bronkosaurus")))))
    spec = Path(playspec.__file__).parent / "specs" / "sidwizard.play"
    assert not cli(["playspec", "check", str(spec), "--binding", str(binding)])
    out = capsys.readouterr().out
    assert out.startswith("ok: sidwizard; order play, voice[2], voice[1], voice[0], commonregs")


def test_entries_are_the_players_play_and_multispeed_calls():
    """PLAYER and MULPLY of the player, as its jump table names them."""
    data = _export("bronkosaurus")
    view = _view(data)
    table = SidImage.from_sid(data).load
    assert playspec.entries(view) == {_word(view, table + 4), _word(view, table + 7)}


@pytest.mark.oracle
def test_every_routine_is_proven_for_every_oracle_tune(tune_path):  # noqa: F811
    _prove_all(_view(tune_path.read_bytes()))


@pytest.mark.oracle
def test_spec_memory_equals_the_6502_for_every_oracle_tune(tune_path):  # noqa: F811
    data = tune_path.read_bytes()
    assert sw_spec_diff.first_difference(data, song_frames(data, None), progress=False) is None


@pytest.mark.oracle
@pytest.mark.parametrize("name", REFUSED_TUNES)
def test_players_the_spec_does_not_state_are_refused(name):
    view = _view(Path(resolve_tune(TUNES[name])).read_bytes())
    with pytest.raises(ValueError, match=REFUSED_TUNES[name]):
        playspec.binding(view)
