"""Disassemble the 6502 code of a SID-Wizard ``.sid`` after its init, with the
playspec binding's addresses as labels.

    python scripts/sw_disasm.py TUNE.sid [START [END]]
"""

import argparse
from pathlib import Path

from pysidtracker import SidImage
from pysidtracker.disasm import disassemble, image_after_init


def _addr(text: str) -> int:
    return int(text.lstrip("$"), 16)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tune", type=Path)
    parser.add_argument("start", type=_addr, nargs="?")
    parser.add_argument("end", type=_addr, nargs="?")
    parser.add_argument("--no-labels", action="store_true", help="skip the binding's labels")
    args = parser.parse_args(argv)
    image = image_after_init(SidImage.from_sid(args.tune.read_bytes()))
    labels = {}
    if not args.no_labels:
        from pysidwizard import playspec  # pylint: disable=import-outside-toplevel

        try:
            found = playspec.labels(bytes(image.window(0, 0x10000)))
        except ValueError as exc:
            print(f"; no binding: {exc}")
        else:
            labels = {name.upper(): addr for name, addr in found.items()}
    for addr, raw, text in disassemble(image, args.start, args.end, labels):
        print(f"{addr:04x}  {raw.hex(' '):<9}  {text}")


if __name__ == "__main__":
    main()
