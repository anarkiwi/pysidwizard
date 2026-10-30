"""Lift a SID-Wizard export's play routine (or a stretch of it) into playspec statements.

Work in progress for the playspec migration (anarkiwi/pysidwizard#114): it runs a
pysidtracker ``native`` hook over the 1.97 export's own 6502 with ``unnative``.

    python scripts/lift_player.py bronkosaurus 1176 --x-step 7

The code bytes the player rewrites are found first (stores into instructions
``walk_code`` reaches, plus those seen written while the tune plays) and declared
as ``operand`` cells, which ``unnative`` needs.
"""

import argparse
import sys
import time
from pathlib import Path

from jennings.devices.mpu6502 import MPU
from pysidtracker import parse_sid_header
from pysidtracker.image import SidImage
from pysidtracker.oracle import EmuPlayer
from pysidtracker.playspec import parse
from pysidtracker.playspec.unnative import unnative
from pysidtracker.relocate import walk_code

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._swm_cache import sw197_export_path  # noqa: E402  pylint: disable=wrong-import-position

STORE_ABS = {0x8D, 0x8E, 0x8C, 0xEE, 0xCE, 0x0E, 0x4E, 0x2E, 0x6E}
STORE_IDX = {0x9D, 0x99, 0xFE, 0xDE, 0x1E, 0x5E, 0x3E, 0x7E}
CODE_END = 0x4000
PLAY_CALLS = 250
STEPS_PER_CALL = 200000


def static_operands(mem: bytes, starts, lo: int) -> set:
    """The bytes of reachable instructions that a reachable store names."""
    code = walk_code(SidImage.from_prg(b"\0\0" + bytes(mem)), starts, lo, CODE_END)
    code_bytes = {a + k for a, n in code.items() for k in range(n)}
    targets = set()
    for address in code:
        if mem[address] in STORE_ABS | STORE_IDX:
            targets.add(mem[address + 1] | mem[address + 2] << 8)
    return targets & code_bytes


class _Written(bytearray):
    def __init__(self, data):
        super().__init__(data)
        self.written = set()

    def __setitem__(self, key, value):
        if isinstance(key, int):
            self.written.add(key)
        super().__setitem__(key, value)


def seen_operands(mem: bytes, play: int, lo: int) -> set:
    """The instruction bytes written while ``PLAY_CALLS`` play calls run."""
    memory, executed = _Written(mem), set()
    cpu = MPU(memory=memory)
    for _ in range(PLAY_CALLS):
        cpu.sp, cpu.pc, depth = 0xFF, play, 0
        for _ in range(STEPS_PER_CALL):
            opcode = memory[cpu.pc]
            executed.update(range(cpu.pc, cpu.pc + 3))
            if opcode == 0x60 and depth == 0:
                break
            depth += {0x20: 1, 0x60: -1}.get(opcode, 0)
            cpu.step()
    return {a for a in memory.written & executed if lo <= a < 0xD000}


def lift(name: str, start: int, x_step: int, resume: int) -> str:
    """The playspec machine ``unnative`` makes of the 6502 from ``start`` to ``resume``."""
    data = sw197_export_path(f"native/examples/sid-exports/{name}.sid").read_bytes()
    header = parse_sid_header(data)
    load = header.real_load_address
    player = EmuPlayer(data[header.data_start :], load, header.init_address, header.play_address, 0)
    player._init(0)  # pylint: disable=protected-access
    mem = bytes(player._mem)  # pylint: disable=protected-access
    operands = sorted(
        static_operands(mem, [header.play_address, header.init_address], load)
        | seen_operands(mem, header.play_address, load)
    )
    cells = "\n".join(f"cell op{i}: u8 @ {a} operand;" for i, a in enumerate(operands))
    argument = f"x = x * {x_step}" if x_step else ""
    spec = parse(
        f"spec lift;\nlayout {{ s, r: addr; }}\n{cells}\n"
        f"machine m[x in 0..1] {{\n  step {{ native L.s({argument}) resumes L.r; }}\n}}\n"
        "schedule { m[0]; }\n"
    )
    out = unnative(spec, {"s": start, "r": resume}, mem)
    return out.source[out.source.index("machine m") :]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("export", help="a 1.97 sid-exports name, e.g. bronkosaurus")
    parser.add_argument("start", help="hex address the hook starts at")
    parser.add_argument("--resume", default="FFFF", help="hex address the stretch ends at")
    parser.add_argument("--x-step", type=int, default=0, help="pass X as instance * N")
    args = parser.parse_args()
    began = time.time()
    text = lift(args.export, int(args.start, 16), args.x_step, int(args.resume, 16))
    print(text)
    print(f"// {len(text.splitlines())} lines in {time.time() - began:.0f} s", file=sys.stderr)


if __name__ == "__main__":
    main()
