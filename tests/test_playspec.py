"""The SID-Wizard playspec against the tune's own 6502 row reader."""

import random

import pytest
from pysidtracker import SidImage
from pysidtracker.disasm import image_after_init
from pysidtracker.playspec import elaborate
from pysidtracker.playspec.tv import replay, validate
from pysidtracker.testing import make_tune_fixtures

from pysidwizard import playspec
from tests._swm_cache import sw197_export_path
from tests.test_oracle import TUNES

EXPORTS = ("bronkosaurus", "euphoria", "flashitback", "glacialblues")
RETURN = 0x60  # RTS: each stretch runs on into code the spec does not state yet
RANDOM_CASES = 300

tune_id, tune_path = make_tune_fixtures(TUNES)


def _rows(mem: bytearray, found: dict, rng: random.Random) -> None:
    """A random pattern at a random pointer and random row-reader state."""
    base = rng.choice([0x4000, 0x40C0, 0x7000])
    mem[found["zp_ptn"]], mem[found["zp_ptn"] + 1] = base & 0xFF, base >> 8
    row_bytes = [0, 0, 1, 0x31, 0x70, 0x71, 0x77, 0x78, 0x80, 0x85, 0x90, 0xB1, 0xC0, 0xFF]
    for offset in range(0x100):
        mem[base + offset] = rng.choice(row_bytes + [rng.randrange(256)])
    mem[found["ptnpos"]] = rng.randrange(256)
    mem[found["nopcnt"]] = rng.choice([0, 0, 0, 1, 2, 5, 9])
    for cell in ("curnote", "curifx", "curfx", "fxval"):
        mem[found[cell]] = rng.randrange(256)


def _ticks(mem: bytearray, found: dict, rng: random.Random) -> None:
    """A random tempo program and tick-clock state."""
    for offset in range(0x100):
        mem[found["tempotbl"] + offset] = rng.choice([0, 1, 2, 3, 5, 0x7F, 0x80, 0x81, 0xFF])
    mem[found["spdcnt"]] = rng.choice([0, 1, 2, 3, rng.randrange(256)])
    mem[found["tmppos"]] = rng.randrange(256)
    mem[found["tmpstart"]] = rng.randrange(256)


# Each stretch of the player the spec states: its routine, its first address, the addresses
# its code leaves by (an RTS goes in each), and a random state for it.
STRETCHES = {
    "read_row": (
        playspec.row_reader,
        lambda view: (playspec.reader_end(view),),
        _rows,
    ),
    "tick": (playspec.clock, playspec.clock_exits, _ticks),
}


def _export(name: str) -> bytes:
    return sw197_export_path(f"native/examples/sid-exports/{name}.sid").read_bytes()


def _view(data: bytes, stretch: str) -> bytearray:
    """The 64 KiB memory after the tune's init, with an RTS where ``stretch`` ends."""
    view = bytearray(image_after_init(SidImage.from_sid(data)).window(0, 0x10000))
    for address in STRETCHES[stretch][1](bytes(view)):
        view[address] = RETURN
    return view


def _prove(data: bytes, stretch: str):
    view = bytes(_view(data, stretch))
    return validate(
        playspec.SPEC,
        playspec.binding(view),
        f"voice[0].{stretch}",
        view,
        STRETCHES[stretch][0](view),
        registers={"X": 0},
        preserve=("X",),
    )


@pytest.mark.parametrize("name", EXPORTS)
def test_the_voices_cells_sit_seven_bytes_apart_from_the_found_addresses(name):
    view = bytes(_view(_export(name), "read_row"))
    found = playspec.binding(view)
    program = elaborate(playspec.SPEC, found)
    for cell_name in ("fxval", "spdcnt"):
        placed = sorted(
            a for cell in program.cells if cell.name == cell_name for a in cell.extent()
        )
        assert placed == [found[cell_name] + 7 * voice for voice in range(3)]


@pytest.mark.parametrize("stretch", STRETCHES)
@pytest.mark.parametrize("name", EXPORTS)
def test_stretch_equals_the_6502_on_random_state(name, stretch):
    """Every random state leaves memory as the 6502 code leaves it."""
    view = _view(_export(name), stretch)
    found = playspec.binding(bytes(view))
    entry = STRETCHES[stretch][0](bytes(view))
    rng = random.Random(name)
    for _ in range(RANDOM_CASES):
        memory = bytearray(view)
        STRETCHES[stretch][2](memory, found, rng)
        assert not replay(
            playspec.SPEC, found, f"voice[0].{stretch}", bytes(memory), entry, {"X": 0}
        )


@pytest.mark.parametrize("stretch", STRETCHES)
@pytest.mark.parametrize("name", EXPORTS)
def test_stretch_is_proven_against_the_6502(name, stretch):
    """The spec routine equals the 6502 code for all initial memory: the same bytes written
    outside the stack page. Which exit the clock takes is not compared, only its writes."""
    result = _prove(_export(name), stretch)
    assert result.proven, result.counterexample and {
        k: v for k, v in result.counterexample.items() if k != "memory"
    }


@pytest.mark.oracle
@pytest.mark.parametrize("stretch", STRETCHES)
def test_stretch_is_proven_for_every_oracle_tune(tune_path, stretch):  # noqa: F811
    assert _prove(tune_path.read_bytes(), stretch).proven
