"""The SID-Wizard playspec against the tune's own 6502 row reader."""

import random

import pytest
from jennings.devices.mpu6502 import MPU
from pysidtracker import SidImage
from pysidtracker.disasm import image_after_init
from pysidtracker.playspec import elaborate, run
from pysidtracker.playspec.tv import validate
from pysidtracker.testing import make_tune_fixtures

from pysidwizard import playspec
from tests._swm_cache import sw197_export_path
from tests.test_oracle import TUNES

EXPORTS = ("bronkosaurus", "euphoria", "flashitback", "glacialblues")
STACK = range(0x100, 0x200)
BUDGET = 20000
RANDOM_CASES = 300

tune_id, tune_path = make_tune_fixtures(TUNES)


def _rows(mem: bytearray, found: dict, rng: random.Random) -> None:
    """A random pattern at a random pointer and random row-reader state."""
    base = rng.choice([0x4000, 0x40C0, 0x7000])
    for field in ("zp_note", "zp_inst", "zp_fx", "zp_value"):
        mem[found[field]], mem[found[field] + 1] = base & 0xFF, base >> 8
    row_bytes = [0, 0, 1, 0x31, 0x70, 0x71, 0x77, 0x78, 0x80, 0x85, 0x90, 0xB1, 0xC0, 0xFF]
    for offset in range(0x100):
        mem[base + offset] = rng.choice(row_bytes + [rng.randrange(256)])
    mem[found["ptnpos"]] = rng.randrange(256)
    mem[found["nopcnt"]] = rng.choice([0, 0, 0, 1, 2, 5, 9])
    for cell in ("curnote", "curifx", "curifx_long", "curfx", "fxval"):
        mem[found[cell]] = rng.randrange(256)


def _ticks(mem: bytearray, found: dict, rng: random.Random) -> None:
    """A random tempo program and tick-clock state."""
    for offset in range(0x100):
        mem[found["tempotbl"] + offset] = rng.choice([0, 1, 2, 3, 5, 0x7F, 0x80, 0x81, 0xFF])
    mem[found["spdcnt"]] = rng.choice([0, 1, 2, 3, rng.randrange(256)])
    mem[found["tmppos"]] = rng.randrange(256)
    mem[found["tmpstart"]] = rng.randrange(256)


# Each stretch of the player the spec states, which runs on into code the spec does not
# state yet: its first address, the address where it ends, and a random state for it.
STRETCHES = {
    "read_row": (playspec.row_reader, playspec.reader_end, _rows),
    "tick": (playspec.clock, playspec.clock_stop, _ticks),
}


def _export(name: str) -> bytes:
    return sw197_export_path(f"native/examples/sid-exports/{name}.sid").read_bytes()


def _view(data: bytes) -> bytes:
    """The 64 KiB memory after the tune's init."""
    return bytes(image_after_init(SidImage.from_sid(data)).window(0, 0x10000))


def _replay(spec, found: dict, stretch: str, memory: bytes, entry: int, stop: int) -> list:
    """The addresses outside the stack page where the spec routine and the jennings run of
    the code from ``entry`` to ``stop`` leave ``memory`` differently."""
    spec_mem = run(
        elaborate(spec, found, routine=f"voice[0].{stretch}"),
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


def _prove(data: bytes, stretch: str):
    view = _view(data)
    spec, found = playspec.bind(view)
    return validate(
        spec,
        found,
        f"voice[0].{stretch}",
        view,
        STRETCHES[stretch][0](view),
        registers={"X": 0},
        preserve=("X",),
        stop=STRETCHES[stretch][1](view),
    )


@pytest.mark.parametrize("name", EXPORTS)
def test_the_voices_cells_sit_seven_bytes_apart_from_the_found_addresses(name):
    spec, found = playspec.bind(_view(_export(name)))
    program = elaborate(spec, found)
    for cell_name in ("fxval", "spdcnt"):
        placed = sorted(
            a for cell in program.cells if cell.name == cell_name for a in cell.extent()
        )
        assert placed == [found[cell_name] + 7 * voice for voice in range(3)]


@pytest.mark.parametrize("stretch", STRETCHES)
@pytest.mark.parametrize("name", EXPORTS)
def test_stretch_equals_the_6502_on_random_state(name, stretch):
    """Every random state leaves memory as the 6502 code leaves it."""
    view = _view(_export(name))
    spec, found = playspec.bind(view)
    entry, stop = STRETCHES[stretch][0](view), STRETCHES[stretch][1](view)
    rng = random.Random(name)
    for _ in range(RANDOM_CASES):
        memory = bytearray(view)
        STRETCHES[stretch][2](memory, found, rng)
        assert not _replay(spec, found, stretch, bytes(memory), entry, stop)


@pytest.mark.parametrize("stretch", STRETCHES)
@pytest.mark.parametrize("name", EXPORTS)
def test_stretch_is_proven_against_the_6502(name, stretch):
    """The spec routine equals the 6502 code up to ``stop`` for all initial memory: the same
    bytes written outside the stack page. The row signal the clock sets is not memory."""
    result = _prove(_export(name), stretch)
    assert result.proven, result.counterexample and {
        k: v for k, v in result.counterexample.items() if k != "memory"
    }


@pytest.mark.oracle
@pytest.mark.parametrize("stretch", STRETCHES)
def test_stretch_is_proven_for_every_oracle_tune(tune_path, stretch):  # noqa: F811
    assert _prove(tune_path.read_bytes(), stretch).proven


@pytest.mark.parametrize("name", EXPORTS)
def test_bind_picks_the_tempo_program_spec_for_a_build_with_tempo_programs(name):
    spec, found = playspec.bind(_view(_export(name)))
    assert spec is playspec.SPEC and "tempo" not in found


def test_simple_variant_states_only_the_single_tempo_byte_clock():
    simple = playspec.SIMPLE_SPEC
    assert simple.base == "sidwizard" and simple.name == "sidwizard_simple"
    cells = {cell.name for cell in elaborate(simple, _simple_layout()).cells}
    assert "tempo_byte" in cells and not cells & {"tmppos", "tmpstart", "tempotbl"}


def _simple_layout() -> dict:
    names = "zp_note zp_inst zp_fx zp_value ptnpos nopcnt curnote curifx curifx_long curfx fxval"
    layout = {name: 0x100 + 8 * i for i, name in enumerate(names.split())}
    layout.update({"spdcnt": 0x200, "tempo": 0x300})
    return layout


@pytest.mark.oracle
def test_bind_picks_the_variant_the_clock_shape_selects(tune_path):  # noqa: F811
    spec, found = playspec.bind(_view(tune_path.read_bytes()))
    assert (spec is playspec.SIMPLE_SPEC) == ("tempo" in found)
