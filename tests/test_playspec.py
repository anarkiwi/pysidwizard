"""The SID-Wizard playspec against the tune's own 6502: routine proofs and whole songs."""

# pylint: disable=redefined-outer-name

import json
import random
import sys
from pathlib import Path

import coverage
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

import sw_spec_diff  # noqa: E402  # pylint: disable=wrong-import-position,wrong-import-order

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


def _first_difference(*args, **kwargs):
    """``sw_spec_diff.first_difference`` with every coverage collector paused (an xdist
    worker runs two): tracing the generated frame function slows SpecPlayer many times
    over and measures no line of the package."""
    paused = []
    while (cov := coverage.Coverage.current()) is not None:
        cov.stop()
        paused.append(cov)
    try:
        return sw_spec_diff.first_difference(*args, progress=False, **kwargs)
    finally:
        for cov in reversed(paused):
            cov.start()


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


def _table_row(found: dict, position: str):
    """The table byte at voice 0's ``position`` through the zero-page pointer, as z3 reads it."""

    def byte(mem0):
        ptr = z3.Concat(z3.Select(mem0, found["zp"] + 1), z3.Select(mem0, found["zp"]))
        return z3.Select(mem0, ptr + z3.ZeroExt(8, z3.Select(mem0, found[position])))

    return byte


def _row_kinds(found: dict, position: str):
    """The four kinds of table row by its first byte, $00..$7F, $80..$FD, $FE, $FF: cases
    that together cover every table."""
    byte = _table_row(found, position)
    return [
        (lambda m, _r: z3.ULT(byte(m), 0x80),),
        (lambda m, _r: z3.And(z3.UGE(byte(m), 0x80), z3.ULT(byte(m), 0xFE)),),
        (lambda m, _r: byte(m) == 0xFE,),
        (lambda m, _r: byte(m) == 0xFF,),
    ]


def routines(found: dict, view: bytes):
    """``(label, routine, pc, stop, cases)`` of each routine of the spec that is a routine
    of the player, from its label, inputs in registers or, for a dispatched effect, the
    effect its column names; a stretch stops where the next routine or a dispatch starts."""
    one = [()]
    out = [
        ("CNTPLY1", "voice[0].at_cntply1", found["cntplay"] + 6, found["cntply2"], one),
        ("VIBSLIDE", "voice[0].vibslide", found["cntply2"], found["filtprg"], one),
        ("FILTPRG", "voice[0].filtprg", found["filtprg"], found["setpwid"], one),
        (
            "SETPWID",
            "voice[0].pw_table",
            found["setpwid"],
            found["wfarptb"],
            _row_kinds(found, "pwtpos"),
        ),
        ("SETVIB0", "voice[0].setvib0", found["setvib0"], None, one),
        ("SETVIB1", "voice[0].setvib1", found["setvib1"], None, one),
        ("SETVIBR", "voice[0].at_setvibr", found["setvibr"], None, one),
        ("SETFMOD", "voice[0].at_setfmod", found["setfmod"], None, one),
        ("FORCVIB", "voice[0].forcvib", found["forcvib"], None, one),
        ("STRTSND", "voice[0].startsound", found["to_strtsnd"], found["setadsr_fx"], one),
        ("SELNTFX", "voice[0].at_selntfx", found["selntfx"], found["indexj1_end"] - 2, one),
        ("SMALPFX", "voice[0].at_smalsel", found["smalpfx"], found["indexj2_end"] - 2, one),
        ("BIGPTFX", "voice[0].at_bigptfx", found["bigptfx"], found["indexjp"] - 1, one),
        ("COMMONREGS", "commonregs.step", found["commonregs"], found["retplay"], one),
    ]
    if not found["chordsupport"]:
        out.append(("WFARPTB", "voice[0].at_wfarptb", found["wfarptb"], None, one))
    for label, routine, flag in (
        ("SEQ_FX", "at_seq_fx", "seq_fx_support"),
        ("SETSEQA", "at_setseqa", "subtunejump"),
        ("TRAKTMP", "at_traktmp", "tempoprgsupp"),
    ):
        if found[flag]:
            out.append((label, f"voice[0].{routine}", found[label.lower()], None, one))
    for k in range(8):
        pc = found["indexj1_end"] + view[found["notefxtbl"] + 0x78 + k]
        if pc != found["insptfx"]:
            cases = [(_holds(found, "curnot", 0x78 + k),)]
            out.append(
                (f"note ${0x78 + k:02x}", "voice[0].at_note_effect", pc, found["insptfx"], cases)
            )
    for n in range(2, 0x10):
        pc = found["indexj2_end"] + view[found["smallfxtbl"] + n]
        cases = [(_holds(found, "curfx2", n, 7, 4),)]
        out.append((f"small ${n:x}x", "voice[0].at_small_effect", pc, None, cases))
    for n in range(1, 0x20):
        pc = _word(view, found["bigfxtable"] + 2 * n)
        cases = [(_holds(found, "curfx2", n),)]
        out.append((f"big ${n:02x}", "voice[0].at_big_effect", pc, None, cases))
    return out


def _prove(
    view: bytes, found: dict, routine: str, pc: int, *, stop=None, assume=()
):  # pylint: disable=too-many-arguments
    """``tv.validate`` of spec ``routine`` against the code at ``pc``, X = 0."""
    return validate(
        playspec.SPEC,
        found,
        routine,
        view,
        pc,
        registers={"X": 0},
        preserve=("X",) if routine.startswith("voice") else (),
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
    diff = replay(playspec.SPEC, found, routine, cex["memory"], pc, regs)
    shown = {k: v for k, v in cex.items() if k not in ("memory", "cells")}
    raise AssertionError(f"{label} at ${pc:04x}: {shown}; replayed differences {diff[:8]}")


# The routine proofs in groups, by their labels' first word.
GROUPS = {
    "routines": (
        "CNTPLY1",
        "SETVIB0",
        "SETVIB1",
        "SETVIBR",
        "SETFMOD",
        "FORCVIB",
        "SELNTFX",
        "SMALPFX",
        "BIGPTFX",
        "COMMONREGS",
        "SEQ_FX",
        "SETSEQA",
        "TRAKTMP",
    ),
    "note": ("note",),
    "small": ("small",),
    "big": ("big",),
}
# The table and sound-start routines, whose proofs take minutes: oracle-marked.
LONG_GROUPS = {"tables": ("VIBSLIDE", "FILTPRG", "SETPWID", "WFARPTB", "STRTSND")}
# The exports whose routines are proven in the default suite: every build of the set.
PROOF_EXPORTS = EXPORTS + ("kicksamule", "cloudlessr", "demo-example")
# The exports whose table routines are proven in the oracle job; the four larger songs'
# proofs outlast its budget.
TABLE_EXPORTS = ("kicksamule", "cloudlessr", "demo-example")


def _prove_group(view: bytes, group: str) -> int:
    """Prove each routine of ``group``; the number proven."""
    found = playspec.binding(view)
    proven = 0
    for label, routine, pc, stop, cases in routines(found, view):
        if label.split()[0] not in {**GROUPS, **LONG_GROUPS}[group]:
            continue
        for assume in cases:
            result = _prove(view, found, routine, pc, stop=stop, assume=assume)
            _assert_proven(result, found, label, routine, pc)
        proven += 1
    return proven


@pytest.mark.parametrize("name", EXPORTS)
def test_the_voices_cells_sit_seven_bytes_apart_from_the_found_addresses(name):
    """Each voice's private cells are the found address plus seven bytes per voice."""
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
    routine = f"voice[0].{stretch}"
    result = _prove(view, found, routine, pc, stop=STRETCHES[stretch][1](found))
    _assert_proven(result, found, stretch, routine, pc)


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("name", PROOF_EXPORTS)
def test_every_routine_is_proven_against_the_6502(name, group):
    """Each routine the spec states equals the player's code from its label for all
    initial memory: the same bytes written outside the stack page, X kept."""
    assert _prove_group(_view(_export(name)), group)


@pytest.mark.parametrize("name", ALL_EXPORTS)
def test_spec_memory_equals_the_6502_over_the_whole_song(name):
    """SpecPlayer over the spec and the tune's own play routine leave the SID registers
    and the 64 KiB outside the stack page equal after every call of the song."""
    data = _export(name)
    if name in REFUSED_EXPORTS:
        with pytest.raises(ValueError, match=REFUSED_EXPORTS[name]):
            playspec.binding(_view(data))
        return
    assert _first_difference(data, song_frames(data, 1)) is None


@pytest.mark.parametrize("name", [n for n in ALL_EXPORTS if n not in REFUSED_EXPORTS])
def test_the_cli_checks_the_spec_with_a_bound_layout(name, tmp_path, capsys):
    """``pysidtracker playspec check`` accepts the spec bound to each export's layout."""
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps(playspec.binding(_view(_export(name)))))
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
@pytest.mark.parametrize("group", LONG_GROUPS)
@pytest.mark.parametrize("name", TABLE_EXPORTS)
def test_table_routines_are_proven_against_the_6502(name, group):
    """The table and sound-start routines from their labels, on every build of the set."""
    assert _prove_group(_view(_export(name)), group)


@pytest.mark.oracle
def test_spec_memory_equals_the_6502_for_every_oracle_tune(tune_path):  # noqa: F811
    """Every call of every stated oracle tune's whole song leaves both players equal."""
    data = tune_path.read_bytes()
    assert _first_difference(data, song_frames(data, None)) is None


@pytest.mark.oracle
@pytest.mark.parametrize("name", ("lykia_boss", "star_storm"))
def test_sound_effects_of_an_sfx_build_equal_the_6502(name):
    """A game's SFXinit calls (SFXsub, the jump table's fifth entry) start sound effects
    on the third voice: every call of the song still leaves both players equal."""
    data = Path(resolve_tune(TUNES[name])).read_bytes()
    sfxsub = SidImage.from_sid(data).load + 12
    calls = {
        f: (sfxsub, (0x18 + f % 0x40, 0x20 + f % 0x30, 1 + f % 5)) for f in range(40, 3000, 97)
    }
    frames = song_frames(data, None)
    assert _first_difference(data, frames, calls=calls) is None


@pytest.mark.oracle
@pytest.mark.parametrize("name", REFUSED_TUNES)
def test_players_the_spec_does_not_state_are_refused(name):
    """The binder names why it does not bind each player the spec does not state."""
    view = _view(Path(resolve_tune(TUNES[name])).read_bytes())
    with pytest.raises(ValueError, match=REFUSED_TUNES[name]):
        playspec.binding(view)
