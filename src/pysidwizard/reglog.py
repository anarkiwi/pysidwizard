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

from pysidtracker.registers import PAL_CYCLES_PER_FRAME, SID_BASE, SID_REG_COUNT
from pysidtracker.reglog import (
    DEFAULT_WRITE_SPACING,
    RegWrite,
    frame_writes,
    read_reglog,
    write_reglog,
)

from .model import SWMFile
from .player import SWMPlayer

__all__ = [
    "DEFAULT_WRITE_SPACING",
    "RegWrite",
    "frame_writes",
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
    PAL play period (``PAL_CYCLES_PER_FRAME // frame_speed``). The per-frame
    ``(reg, val)`` writes -- absolute ``$D400..$D418`` addresses in
    :meth:`SWMPlayer.play_frame` emit order -- are framed by
    :func:`frame_writes`, which rebases each to a ``0..0x18`` offset, masks the
    value, and spaces writes ``write_spacing`` cycles apart within the frame.
    """
    if player is None:
        player = SWMPlayer(song)
    if cycles_per_frame is None:
        cycles_per_frame = PAL_CYCLES_PER_FRAME // max(1, song.frame_speed)
    per_frame = (player.play_frame() for _ in range(max_frames))
    return frame_writes(
        per_frame,
        cycles_per_frame=cycles_per_frame,
        write_spacing=write_spacing,
        sid_reg_base=SID_BASE,
        reg_count=SID_REG_COUNT,
    )
