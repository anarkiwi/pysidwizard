"""Play a SID-Wizard tune with the playspec and with its own 6502, call by call, and
report the first call whose audible output differs: the SID registers (the bits the chip ignores
masked), and per voice whether its gate was cleared and set again or TEST set within the call.

    python scripts/sw_spec_diff.py TUNE.sid [--frames N] [--song S]
"""

import argparse
import sys
from pathlib import Path

from pysidtracker import SidImage
from pysidtracker.disasm import image_after_init
from pysidtracker.header import parse_sid_header
from pysidtracker.oracle import EmuPlayer
from pysidtracker.playspec import SpecPlayer
from pysidtracker.testing import song_frames
from tqdm import tqdm
from vagrant_moses.cpu import run_to_rts

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pysidwizard import playspec  # noqa: E402  # pylint: disable=wrong-import-position

UNUSED = {3: 0x0F, 10: 0x0F, 17: 0x0F, 21: 0x07}
MAX_CYCLES = 200_000


def play_entry(image: SidImage) -> int:
    """The PLAYER an interrupt-driven tune's own interrupt calls: the play entry, after the
    tune's init, that binds (a copy the init left behind addresses the moved player)."""
    view = bytes(image_after_init(image).window(0, 0x10000))
    for entry in sorted(playspec.entries(view)):
        try:
            if not playspec.binding(view, entry)["mulply"]:
                return entry
        except ValueError:
            continue
    raise ValueError("no SID-Wizard play entry binds after the tune's init")


def players(data: bytes, song: int = 1):
    """``(spec, cpu)``: the playspec's SpecPlayer and the tune's own EmuPlayer, each
    calling the header's play address, or :func:`play_entry` for a tune without one, as
    a PSID driver calls it."""
    image = SidImage.from_bytes(data)
    header = parse_sid_header(data)
    play = header.play_address or play_entry(image)
    args = (image.image, image.load, header.init_address, play, song - 1)
    spec = SpecPlayer(
        playspec.SPEC,
        *args,
        bind=playspec.binder(),
        entries=playspec.entries,
        psid=header.is_psid or not header.play_address,
    )
    return spec, EmuPlayer(*args)


def call(player, addr: int, regs) -> None:
    """Run the 6502 subroutine at ``addr`` with ``(A, X, Y)`` on ``player``'s machine."""
    mpu = player._mpu  # pylint: disable=protected-access
    mpu.x, mpu.y = regs[1], regs[2]
    if isinstance(player, SpecPlayer):
        mpu.a = regs[0]
        player._call(addr)  # pylint: disable=protected-access
        return
    run_to_rts(mpu, player._obs, addr, regs[0], MAX_CYCLES)  # pylint: disable=protected-access


def audible(writes, regs, ctrl):
    """One call's registers with the bits the chip ignores masked, and per voice whether its gate
    was cleared and set again and whether TEST was set within the call; ``ctrl`` (each voice's
    control register at the end of the call before) is updated."""
    edges = []
    for v in range(3):
        seen = [ctrl[v]] + [val for r, val in writes if r == 4 + 7 * v]
        ctrl[v] = regs[4 + 7 * v]
        edges.append((bool(ctrl[v] & 1) and any(not s & 1 for s in seen), any(s & 8 for s in seen)))
    return tuple(r & UNUSED.get(i, 0xFF) for i, r in enumerate(regs)), tuple(edges)


def first_difference(data: bytes, frames: int, song: int = 1, progress: bool = True, calls=None):
    """``(frame, spec output, 6502 output)`` of the first call the two players leave sounding
    differently, or None; ``calls`` maps a frame to ``(addr, (A, X, Y))``, a subroutine both
    run first."""
    spec, cpu = players(data, song)
    ctrl_spec, ctrl_cpu = [0] * 3, [0] * 3
    for frame in tqdm(range(frames), desc="calls", unit="call", disable=not progress):
        if calls and frame in calls:
            for player in (spec, cpu):
                call(player, *calls[frame])
        mine = audible(spec.play_frame(), spec.snapshot(), ctrl_spec)
        real = audible(cpu.play_frame(), cpu.snapshot(), ctrl_cpu)
        if mine != real:
            return frame, mine, real
    return None


def report(frame: int, mine, real) -> None:
    """Print the differing call's registers and edges."""
    print(f"call {frame} differs")
    print(f"  spec {mine[0]} {mine[1]}\n  6502 {real[0]} {real[1]}")


def main(argv=None) -> None:
    """Parse ``argv`` and run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tune", type=Path)
    parser.add_argument("--frames", type=int)
    parser.add_argument("--song", type=int, default=1)
    args = parser.parse_args(argv)
    data = args.tune.read_bytes()
    frames = args.frames or song_frames(data, args.song)
    found = first_difference(data, frames, args.song)
    if found is None:
        print(f"{args.tune.name}: {frames} calls equal")
        return
    report(*found)


if __name__ == "__main__":
    main()
