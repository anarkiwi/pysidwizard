"""One-shot: bring up VICE+SID-Wizard, halt at song-start, dump player
code at $1010..$2010 to ``/tmp/player_code.bin`` for offline inspection.

Used to figure out what WRPITCH looks like in the actual build — the
v0.4.1 discovery signature ``B5 10 75 ?? (08|9D)`` doesn't match the
editor's loaded code, so we need to look at the raw bytes.
"""

from __future__ import annotations

import os
import sys
import tempfile

from sidwizard_driver.fetch import fetch_disk1_d64
from sidwizard_driver.ghost_dump import (
    PLAYER_ENTRY,
    SELFMOD_SCAN_LEN,
    SELFMOD_SCAN_START,
    VICE_STATE_DIR,
)
from sidwizard_driver.sidwizard import Sidwizard
from vice_driver import BinMon, DiskMount, ViceContainer


def main() -> int:
    d64 = str(fetch_disk1_d64())
    sys.path.insert(0, str(__import__("os").path.dirname(__import__("os").path.dirname(__file__))))
    from tests._swm_cache import swm_path  # noqa: E402

    swm = str(swm_path("flashitback"))
    out_path = "/tmp/player_code.bin"

    host_work = tempfile.mkdtemp(prefix="dump-player-")
    host_vice = tempfile.mkdtemp(prefix="dump-player-vice-")
    host_swm_d64 = os.path.join(host_work, "tune.d64")

    mounts = [
        DiskMount(host_path=d64, container_path="/tmp/sidwizard-editor.d64", read_only=True),
        DiskMount(host_path=host_work, container_path="/tmp/sidwizard-driver", read_only=False),
        DiskMount(host_path=host_vice, container_path=VICE_STATE_DIR, read_only=False),
    ]

    container = ViceContainer(
        image="anarkiwi/headlessvice:latest",
        entrypoint="x64sc",
        binmon_port=6502,
        autostart="/tmp/sidwizard-editor.d64",
        mounts=mounts,
        warp=True,
    )

    with container:
        with BinMon(port=6502) as bm:
            bm.exit()
            sw = Sidwizard(bm)
            tuneheader = sw.wait_for_idle(timeout=60.0)
            print(f"TUNEHEADER = ${tuneheader:04X}")

            sw.load_swm_via_menu(
                swm_path=swm,
                host_d64_path=host_swm_d64,
                container_d64_path="/tmp/sidwizard-driver/tune.d64",
                tuneheader=tuneheader,
                load_timeout=10.0,
            )

            pre_cp = bm.checkpoint_set(PLAYER_ENTRY, stop_when_hit=True)
            sw.play()
            bm.checkpoint_delete(pre_cp.checknum)

            # Run one player tick so we're in steady-state code.
            bm.run_until_pc(PLAYER_ENTRY)
            with bm.halted():
                code = bm.mem_get(
                    SELFMOD_SCAN_START,
                    SELFMOD_SCAN_START + SELFMOD_SCAN_LEN - 1,
                )

    with open(out_path, "wb") as fp:
        fp.write(bytes(code))
    print(f"wrote {len(code)} bytes to {out_path} (base ${SELFMOD_SCAN_START:04X})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
