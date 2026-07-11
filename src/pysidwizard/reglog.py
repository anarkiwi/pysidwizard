"""SID register write logs (the shared ``py*`` register-log convention).

The ``RegWrite`` / ``read_reglog`` / ``write_reglog`` (de)serialisation and the
per-frame :func:`frame_writes` framing loop are shared across the ``py*``
format packages and live in :mod:`pysidtracker.reglog`; this module re-exports
them and adds the SID-Wizard-specific :func:`iter_register_writes`, a thin
wrapper that drives :class:`~pysidwizard.player.SWMPlayer` and feeds its
per-frame ``(reg, val)`` output to :func:`frame_writes`.

``iter_register_writes`` yields :class:`RegWrite(clock, reg, val)`: ``clock`` is
the cycle of the frame (``frame_idx * cycles_per_frame``, framed at the PAL play
period), ``reg`` the SID register offset ``0..0x18`` (``$D400`` subtracted),
``val`` ``0..255``. Writes are NOT deduped -- every register written by
:meth:`SWMPlayer.play_frame` for the frame is yielded, so a forward-filling
framer reconstructs the exact per-frame register state.
"""

from __future__ import annotations

from typing import Iterator, Optional

from pysidtracker.registers import PAL_CYCLES_PER_FRAME
from pysidtracker.reglog import (
    DEFAULT_WRITE_SPACING,
    RegWrite,
    read_reglog,
    register_writes_from_player,
    write_reglog,
)

from .model import SWMFile
from .player import SWMPlayer

__all__ = [
    "DEFAULT_WRITE_SPACING",
    "RegWrite",
    "iter_register_writes",
    "read_reglog",
    "write_reglog",
]


def iter_register_writes(
    song: SWMFile,
    max_frames: int = 50 * 60,
    cycles_per_frame: Optional[int] = None,
    write_spacing: int = DEFAULT_WRITE_SPACING,
    player: Optional[SWMPlayer] = None,
) -> Iterator[RegWrite]:
    """Yield :class:`RegWrite` for ``song``, frame by frame.

    ``song`` is a parsed :class:`~pysidwizard.model.SWMFile`; a fresh
    :class:`SWMPlayer` is built from it unless ``player`` is supplied.
    SID-Wizard's player loops forever, so ``max_frames`` bounds the log
    (default one minute at 50 Hz). ``cycles_per_frame`` defaults to the song's
    PAL play period (``PAL_CYCLES_PER_FRAME // frame_speed``).

    :class:`SWMPlayer` is a :class:`~pysidtracker.player.MemPlayer`, so this is
    a thin wrapper over the shared
    :func:`~pysidtracker.reglog.register_writes_from_player`: it emits the
    post-init register baseline at clock 0, then each frame's changed
    ``0..0x18`` register writes ``write_spacing`` cycles apart.
    """
    if player is None:
        player = SWMPlayer(song)
    if cycles_per_frame is None:
        cycles_per_frame = PAL_CYCLES_PER_FRAME // max(1, song.frame_speed)
    return register_writes_from_player(
        player,
        max_frames=max_frames,
        cycles_per_frame=cycles_per_frame,
        write_spacing=write_spacing,
    )
