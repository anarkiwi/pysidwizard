"""Play a SID-Wizard tune with the playspec and with its own 6502, call by call, and
report the first call whose SID writes or memory (outside the stack page) differ.

    python scripts/sw_spec_diff.py TUNE.sid [--frames N] [--song S]
"""

import argparse
import sys
from pathlib import Path

from pysidtracker import SidImage
from pysidtracker.header import parse_sid_header
from pysidtracker.oracle import EmuPlayer
from pysidtracker.playspec import SpecPlayer
from pysidtracker.testing import song_frames
from tqdm import tqdm
from vagrant_moses.cpu import run_to_rts

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pysidwizard import playspec  # noqa: E402  # pylint: disable=wrong-import-position

STACK = range(0x100, 0x200)
MAX_CYCLES = 200_000


def players(data: bytes, song: int = 1):
    """``(spec, cpu)``: the playspec's SpecPlayer and the tune's own EmuPlayer."""
    image = SidImage.from_bytes(data)
    header = parse_sid_header(data)
    args = (image.image, image.load, header.init_address, header.play_address, song - 1)
    spec = SpecPlayer(
        playspec.SPEC,
        *args,
        bind=playspec.binder(),
        entries=playspec.entries,
        psid=header.is_psid,
        sid=image if header.play_address == 0 else None,
    )
    return spec, EmuPlayer(*args)


def call(player, addr: int, regs) -> None:
    """Run the 6502 subroutine at ``addr`` with ``(A, X, Y)`` on ``player``'s machine."""
    mpu = player._mpu  # pylint: disable=protected-access
    mpu.x, mpu.y = regs[1], regs[2]
    run_to_rts(mpu, player._obs, addr, regs[0], MAX_CYCLES)  # pylint: disable=protected-access


def first_difference(data: bytes, frames: int, song: int = 1, progress: bool = True, calls=None):
    """``(frame, addresses, players)`` of the first call the two players leave differently,
    or None; ``calls`` maps a frame to ``(addr, (A, X, Y))``, a subroutine both run first."""
    spec, cpu = players(data, song)
    for frame in tqdm(range(frames), desc="calls", unit="call", disable=not progress):
        if calls and frame in calls:
            for player in (spec, cpu):
                call(player, *calls[frame])
        spec.play_frame()
        cpu.play_frame()
        mine, real = spec.memory, bytes(cpu._mem)  # pylint: disable=protected-access
        same = (
            mine[: STACK.start] == real[: STACK.start] and mine[STACK.stop :] == real[STACK.stop :]
        )
        if not same or spec.regs != cpu.regs:
            diff = [a for a in range(0x10000) if mine[a] != real[a] and a not in STACK]
            return frame, diff, (spec, cpu)
    return None


def report(spec, cpu, frame: int, diff) -> None:
    """Print the differing call's addresses, by the binding's names, and the registers."""
    names = {}
    for name, addr in playspec.labels(spec.view(), spec.entry).items():
        for k in range(0, 3 * playspec.VOICE_STRIDE, playspec.VOICE_STRIDE):
            names.setdefault(addr + k, f"{name}+{k}" if k else name)
    mine, real = spec.memory, bytes(cpu._mem)  # pylint: disable=protected-access
    print(f"call {frame} differs")
    for addr in diff[:24]:
        print(
            f"  ${addr:04x} {names.get(addr, ''):<16} spec ${mine[addr]:02x} 6502 ${real[addr]:02x}"
        )
    print(f"  regs spec {spec.regs}\n  regs 6502 {cpu.regs}")


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
    frame, diff, (spec, cpu) = found
    report(spec, cpu, frame, diff)


if __name__ == "__main__":
    main()
