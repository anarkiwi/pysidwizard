"""The SID-Wizard playspec against the tune's own 6502: routine proofs and whole songs."""

# pylint: disable=redefined-outer-name

import itertools
import json
import subprocess
import sys
from pathlib import Path

import pytest
import z3
from pysidtracker import SidImage
from pysidtracker.disasm import image_after_init
from pysidtracker.maincli import main as cli
from pysidtracker.playspec import elaborate
from pysidtracker.playspec.tv import replay, validate
from pysidtracker.testing import make_tune_fixtures, resolve_tune, song_frames

from pysidwizard import playspec
from tests._refused import REASONS, REFUSED
from tests._swm_cache import SW197_EXPORT_SHA256, sw197_export_path
from tests.test_oracle import TUNES

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import sw_spec_diff  # noqa: E402  # pylint: disable=wrong-import-position,wrong-import-order

EXPORTS = ("bronkosaurus", "euphoria", "flashitback", "glacialblues")
_SID_EXPORTS = "native/examples/sid-exports/"
ALL_EXPORTS = tuple(Path(m).stem for m in SW197_EXPORT_SHA256 if m.startswith(_SID_EXPORTS))
STACK = range(0x100, 0x200)
BUDGET = 20000
# Players the binder refuses, by the reason it gives.
REFUSED_EXPORTS = {name: "multi-SID" for name in ALL_EXPORTS if name.endswith("SID")}
REFUSED_TUNES = {k: REASONS[REFUSED[v][1]] for k, v in TUNES.items() if 1 in REFUSED.get(v, {})}
STATED_TUNES = {k: v for k, v in TUNES.items() if k not in REFUSED_TUNES}

tune_id, tune_path = make_tune_fixtures(STATED_TUNES)


def _export(name: str) -> bytes:
    return sw197_export_path(f"{_SID_EXPORTS}{name}.sid").read_bytes()


def _view(data: bytes) -> bytes:
    """The whole memory after the tune's init."""
    return bytes(image_after_init(SidImage.from_sid(data)).window(0, 0x10000))


# Stretches whose code runs on into code they do not state: first address, end.
STRETCHES = {
    "read_row": (lambda b: b["read_row"], lambda b: b["reader_end"]),
    "tick": (lambda b: b["dotrack"], lambda b: b["tick_0"] - 7),
}


def _holds(found: dict, column: str, value: int, high: int = 7, low: int = 0):
    """An assumption that bits ``high..low`` of voice 0's ``column`` hold ``value``."""
    return lambda mem0, _regs: z3.Extract(high, low, z3.Select(mem0, found[column])) == value


def _word(view: bytes, at: int) -> int:
    return view[at] | view[at + 1] << 8


def _table_row(found: dict, position: str, column: int = 0):
    """The byte ``column`` past voice 0's table ``position`` through the zero-page pointer,
    as z3 reads it."""

    def byte(mem0):
        ptr = z3.Concat(z3.Select(mem0, found["zp"] + 1), z3.Select(mem0, found["zp"]))
        return z3.Select(mem0, ptr + z3.ZeroExt(8, z3.Select(mem0, found[position]) + column))

    return byte


def _row_kinds(found: dict, position: str, counter: str = ""):
    """The kinds of table row by its first byte, $00..$7F, $80..$FD, $FE, $FF: cases that
    together cover every table. With the sweep ``counter`` cell, a $00..$7F row splits by
    the code's branches: the sweep ended (its byte equals the counter), or a sweep step of
    either sign."""
    byte = _table_row(found, position)
    step = _table_row(found, position, 1)

    def sweep(m):
        return z3.ULT(byte(m), 0x80)

    kinds = [sweep]
    if counter:
        kinds = [
            lambda m: z3.And(sweep(m), byte(m) == z3.Select(m, found[counter])),
            lambda m: z3.And(
                sweep(m), byte(m) != z3.Select(m, found[counter]), z3.ULT(step(m), 0x80)
            ),
            lambda m: z3.And(
                sweep(m), byte(m) != z3.Select(m, found[counter]), z3.UGE(step(m), 0x80)
            ),
        ]
    kinds += [
        lambda m: z3.And(z3.UGE(byte(m), 0x80), z3.ULT(byte(m), 0xFE)),
        lambda m: byte(m) == 0xFE,
        lambda m: byte(m) == 0xFF,
    ]
    return [(lambda m, _r, k=k: k(m),) for k in kinds]


def _satisfiable(cases: list) -> list:
    """The ``cases`` some memory satisfies."""
    mem = z3.Array("cases", z3.BitVecSort(16), z3.BitVecSort(8))
    return [c for c in cases if z3.Solver().check(c[0](mem, {})) == z3.sat]


def _slidevib_kinds(found: dict):
    """VIBSLIDE's branches by voice 0's SLIDEVIB (vibrato type 0, vibrato, slide down, slide
    up, portamento, with PORTAVIBRA the note portamento $FF apart), a portamento further by
    its direction to the note's frequency and the carries out of its low-byte difference,
    comparison and step: the satisfiable cases, which together cover every state."""

    def cell(m, name):
        return z3.Select(m, found[name])

    def target(m, table):
        return z3.Select(m, found[table] + z3.ZeroExt(8, cell(m, "dpitch")))

    def holds(lo: int, hi: int):
        return lambda m: z3.And(z3.UGE(cell(m, "slidevib"), lo), z3.ULE(cell(m, "slidevib"), hi))

    def below(m):
        now = z3.Concat(cell(m, "freqhi"), cell(m, "freqlo"))
        return z3.ULT(z3.Concat(target(m, "freqtbh"), target(m, "freqtbl")), now)

    def low_borrow(m):
        return z3.ULT(target(m, "freqtbl"), cell(m, "freqlo"))

    def reach_carry(m):
        diff, step = target(m, "freqtbl") - cell(m, "freqlo"), cell(m, "freqmodl")
        return z3.If(below(m), z3.ULT(diff + step, diff), z3.ULT(diff, step))

    def step_carry(m):
        lo, step = cell(m, "freqlo"), cell(m, "freqmodl")
        return z3.If(below(m), z3.ULT(lo, step), z3.ULT(lo + step, lo))

    def case(*conds):
        return (lambda m, _r: z3.And(*(c(m) for c in conds)),)

    cases = [case(holds(lo, hi)) for lo, hi in ((0, 0), (1, 0x7F), (0x82, 0x82), (0x80, 0x81))]
    portamentos = ((0xFF, 0xFF), (0x83, 0xFE)) if found["portavibra"] else ((0x83, 0xFF),)
    for (lo, hi), signs in itertools.product(
        portamentos, itertools.product((True, False), repeat=4)
    ):
        conds = [
            c if sign else (lambda m, c=c: z3.Not(c(m)))
            for c, sign in zip((below, low_borrow, reach_carry, step_carry), signs, strict=True)
        ]
        cases.append(case(holds(lo, hi), *conds))
    return _satisfiable(cases)


def _instrument_in_data(found: dict):
    """The case where the instrument PLAYERZP addresses lies in the module's data: above
    its instrument pointer tables and below the I/O area, so its bytes alias no player
    cell, operand or chip register (as in every player the editor or SID-Maker builds)."""
    tables = [found[k] for k in found if k.startswith(("p_insl", "p_insh")) and found[k]]
    low = min(tables) if tables else 0

    def holds(m, _r):
        ptr = z3.Concat(z3.Select(m, found["zp"] + 1), z3.Select(m, found["zp"]))
        return z3.And(z3.UGE(ptr, low), z3.ULT(ptr, 0xD000 - 0x200))

    return [(holds,)]


def routines(found: dict, view: bytes):
    """``(label, routine, pc, stop, cases)`` of each routine of the spec that is a routine
    of the player, from its label, inputs in registers or, for a dispatched effect, the
    effect its column names; a stretch stops where the next routine or a dispatch starts."""
    one = [()]
    out = [
        ("CNTPLY1", "voice[0].at_cntply1", found["cntplay"] + 6, found["cntply2"], one),
        (
            "VIBSLIDE",
            "voice[0].vibslide",
            found["cntply2"],
            found["filtprg"],
            _slidevib_kinds(found),
        ),
        (
            "FILTPRG",
            "voice[0].filtprg",
            found["filtprg"],
            found["setpwid"],
            _row_kinds(found, "fltposi", "cwepcnt"),
        ),
        (
            "SETPWID",
            "voice[0].at_pw_rows",
            found["setpwid"],
            found["endpwtb"],
            _row_kinds(found, "pwtpos", "pweepcnt"),
        ),
        ("ENDPWTB", "voice[0].at_endpwtb", found["endpwtb"], found["wfarptb"], one),
        ("SETVIB0", "voice[0].setvib0", found["setvib0"], None, one),
        ("SETVIB1", "voice[0].setvib1", found["setvib1"], None, one),
        ("SETVIBR", "voice[0].at_setvibr", found["setvibr"], None, one),
        ("SETFMOD", "voice[0].at_setfmod", found["setfmod"], None, one),
        ("FORCVIB", "voice[0].forcvib", found["forcvib"], None, one),
        (
            "STRTSND",
            "voice[0].startsound",
            found["to_strtsnd"],
            found["setadsr_fx"],
            _instrument_in_data(found),
        ),
        ("SELNTFX", "voice[0].at_selntfx", found["selntfx"], found["indexj1_end"] - 2, one),
        ("SMALPFX", "voice[0].at_smalsel", found["smalpfx"], found["indexj2_end"] - 2, one),
        ("BIGPTFX", "voice[0].at_bigptfx", found["bigptfx"], found["indexjp"] - 1, one),
        ("COMMONREGS", "commonregs.step", found["chipregs"], found["retplay"], one),
    ]
    if found["subtune_restarts"]:
        out.append(("SETSTUNE", "subtune.at_setstune", found["setstune"], None, one))
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
        "SETSTUNE",
        "SEQ_FX",
        "SETSEQA",
        "TRAKTMP",
    ),
    "note": ("note",),
    "small": ("small",),
    "big": ("big",),
}
# The table and sound-start routines, whose proofs take minutes, by their shards of cases:
# oracle-marked, each shard in its own interpreter, since z3's search (so a proof's time)
# hangs on every term made before it.
LONG_ROUTINES = {
    "VIBSLIDE": 4,
    "FILTPRG": 1,
    "SETPWID": 1,
    "ENDPWTB": 1,
    "WFARPTB": 1,
    "STRTSND": 1,
}
CENSUS = Path(__file__).resolve().parent.parent / "scripts" / "sw_proof_census.py"
# The exports whose routines are proven in the default suite: every build of the set.
PROOF_EXPORTS = EXPORTS + ("kicksamule", "cloudlessr", "demo-example", "echo-test")
# A SID-Wizard 1.0 player, whose routines are proven in the oracle job.
V10_TUNE = "pantheon"
# The exports whose table routines are proven in the oracle job; the four larger songs'
# proofs outlast its budget.
TABLE_EXPORTS = ("kicksamule", "cloudlessr", "demo-example")


def _tune_view(name: str) -> bytes:
    return _view(Path(resolve_tune(TUNES[name])).read_bytes())


def _prove_group(view: bytes, group: str) -> int:
    """Prove each routine of ``group``; the number proven."""
    found = playspec.binding(view)
    proven = 0
    for label, routine, pc, stop, cases in routines(found, view):
        if label.split()[0] not in GROUPS[group]:
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
    and the whole memory outside the stack page equal after every call of the song."""
    data = _export(name)
    if name in REFUSED_EXPORTS:
        with pytest.raises(ValueError, match=REFUSED_EXPORTS[name]):
            playspec.binding(_view(data))
        return
    assert sw_spec_diff.first_difference(data, song_frames(data, 1), progress=False) is None


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


def _long_cases(names):
    return [
        pytest.param(name, label, k, n, id=f"{name}-{label}-{k}/{n}")
        for name in names
        for label, n in LONG_ROUTINES.items()
        for k in range(n)
        if not (name == V10_TUNE and label == "WFARPTB")
    ]


@pytest.mark.oracle
@pytest.mark.parametrize("name,label,shard,shards", _long_cases(TABLE_EXPORTS + (V10_TUNE,)))
def test_table_routines_are_proven_against_the_6502(name, label, shard, shards):
    """The table and sound-start routines from their labels, on every build of the set
    and a 1.0 player (WFARPTB, proven on builds without chords, aside)."""
    out = subprocess.run(
        [sys.executable, str(CENSUS), name, "--only", label, "--shard", f"{shard}/{shards}"]
        + ["--jobs", "1"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stdout[-4000:] + out.stderr[-4000:]


@pytest.mark.oracle
@pytest.mark.parametrize("group", GROUPS)
def test_every_routine_of_a_v10_player_is_proven_against_the_6502(group):
    """SID-Wizard 1.0's routines, their release differences included, from their labels."""
    assert _prove_group(_tune_view(V10_TUNE), group)


@pytest.mark.oracle
@pytest.mark.skip(reason="the 1.0 voice restart proof does not finish (#126)")
def test_a_v10_voice_restart_is_proven_against_the_6502():
    """1.0's subtune restart of the last voice (X = 14), up to the next voice's, for an
    orderlist that starts with at most one sequence effect."""
    view = _tune_view(V10_TUNE)
    found = playspec.binding(view)
    pc = found["restart"] + 2
    orderlist = _word(view, found["seqsub"] + 15)

    def fx(mem0, offset):
        byte = z3.Select(mem0, orderlist + offset)
        return z3.And(z3.UGE(byte, 0x80), z3.ULT(byte, 0xFE))

    result = validate(
        playspec.SPEC,
        found,
        "voice[2].restart",
        view,
        pc,
        registers={"X": 14},
        preserve=("X",),
        stop=found["restart_next"],
        assume=(lambda mem0, _regs: z3.Not(z3.And(fx(mem0, 0), fx(mem0, 1))),),
    )
    _assert_proven(result, found, "RESTART", "voice[2].restart", pc)


@pytest.mark.oracle
def test_spec_memory_equals_the_6502_for_every_oracle_tune(tune_path):  # noqa: F811
    """Every call of every stated oracle tune's whole song leaves both players equal."""
    data = tune_path.read_bytes()
    assert sw_spec_diff.first_difference(data, song_frames(data, None), progress=False) is None


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
    assert sw_spec_diff.first_difference(data, frames, calls=calls, progress=False) is None


@pytest.mark.oracle
@pytest.mark.parametrize("name", REFUSED_TUNES)
def test_players_the_spec_does_not_state_are_refused(name):
    """The binder names why it does not bind each player the spec does not state."""
    view = _tune_view(name)
    with pytest.raises(ValueError, match=REFUSED_TUNES[name]):
        playspec.binding(view)
