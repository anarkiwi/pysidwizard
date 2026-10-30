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
RETURN = 0x60  # RTS: the row reader runs on into the next routine, which the spec does not state
RANDOM_ROWS = 300

tune_id, tune_path = make_tune_fixtures(TUNES)


def _view(data: bytes) -> bytearray:
    """The 64 KiB memory after the tune's init, with an RTS after the row reader."""
    view = bytearray(image_after_init(SidImage.from_sid(data)).window(0, 0x10000))
    view[playspec.reader_end(bytes(view))] = RETURN
    return view


def _export(name: str) -> bytes:
    return sw197_export_path(f"native/examples/sid-exports/{name}.sid").read_bytes()


def _random_rows(view: bytearray, found: dict, rng: random.Random) -> bytes:
    """``view`` with a random pattern at a random pointer and random reader state."""
    mem = bytearray(view)
    base = rng.choice([0x4000, 0x40C0, 0x7000])
    mem[found["zp_ptn"]], mem[found["zp_ptn"] + 1] = base & 0xFF, base >> 8
    row_bytes = [0, 0, 1, 0x31, 0x70, 0x71, 0x77, 0x78, 0x80, 0x85, 0x90, 0xB1, 0xC0, 0xFF]
    for offset in range(0x100):
        mem[base + offset] = rng.choice(row_bytes + [rng.randrange(256)])
    mem[found["ptnpos"]] = rng.randrange(256)
    mem[found["nopcnt"]] = rng.choice([0, 0, 0, 1, 2, 5, 9])
    for cell in ("curnote", "curifx", "curfx", "fxval"):
        mem[found[cell]] = rng.randrange(256)
    return bytes(mem)


@pytest.mark.parametrize("name", EXPORTS)
def test_the_voices_cells_sit_seven_bytes_apart_from_the_found_addresses(name):
    view = bytes(_view(_export(name)))
    found = playspec.binding(view)
    program = elaborate(playspec.SPEC, found)
    placed = sorted(a for cell in program.cells if cell.name == "fxval" for a in cell.extent())
    assert placed == [found["fxval"] + 7 * voice for voice in range(3)]


@pytest.mark.parametrize("name", EXPORTS)
def test_row_reader_equals_the_6502_on_random_rows(name):
    """Every random pattern and reader state leaves memory as the 6502 code leaves it."""
    view = _view(_export(name))
    found = playspec.binding(bytes(view))
    rng = random.Random(name)
    for _ in range(RANDOM_ROWS):
        memory = _random_rows(view, found, rng)
        assert not replay(
            playspec.SPEC,
            found,
            "voice[0].read_row",
            memory,
            playspec.row_reader(memory),
            {"X": 0},
        )


@pytest.mark.parametrize("name", EXPORTS)
def test_row_reader_is_proven_against_the_6502(name):
    """``voice[0].read_row`` equals the 6502 row reader for all initial memory."""
    view = bytes(_view(_export(name)))
    result = validate(
        playspec.SPEC,
        playspec.binding(view),
        "voice[0].read_row",
        view,
        playspec.row_reader(view),
        registers={"X": 0},
        preserve=("X",),
    )
    assert result.proven, result.counterexample and {
        k: v for k, v in result.counterexample.items() if k != "memory"
    }


@pytest.mark.oracle
def test_row_reader_is_proven_for_every_oracle_tune(tune_path):  # noqa: F811
    view = bytes(_view(tune_path.read_bytes()))
    result = validate(
        playspec.SPEC,
        playspec.binding(view),
        "voice[0].read_row",
        view,
        playspec.row_reader(view),
        registers={"X": 0},
        preserve=("X",),
    )
    assert result.proven
