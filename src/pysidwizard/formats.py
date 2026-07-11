"""Register SID-Wizard with the generic ``pysidtracker`` command-line tool.

Installing pysidwizard exposes a :class:`~pysidtracker.formats.SidFormat` on the
``pysidtracker.formats`` entry-point group, so ``pysidtracker info|reglog|wav``
work on SID-Wizard ``.sid`` tunes with no bespoke binary: the format's parser
(:class:`~pysidwizard.sidreader.SidWizardSidParser`) recognises and decodes the
tune, and its player (:class:`~pysidwizard.player.SWMPlayer`, a
:class:`~pysidtracker.player.MemPlayer`) renders it through the shared
``reglog`` / ``wav`` commands.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from pysidtracker import SidFormat

from .model import SWMFile
from .player import SWMPlayer
from .sidreader import SidWizardSidParser


def _metadata(swm: SWMFile) -> Sequence:
    """The shared info block for an SWM model: name/author/released/load/init/play.

    The song name, release date, and exact init/play addresses live in the
    ``.sid`` container header rather than the recovered SWM model, so the author
    and load address are surfaced and the remaining fields fall back to blank /
    the load address.
    """
    load = swm.load_address or 0
    author = swm.author_str()
    return (author or "SID-Wizard tune", author, "", load, load, load)


def _describe(swm: SWMFile) -> Iterable[str]:
    """Extra ``info`` lines specific to a SID-Wizard tune."""
    yield f"subtunes: {swm.subtune_count}"
    yield f"driver:   {swm.driver_type}"


FORMAT = SidFormat(
    name="sidwizard",
    parser=SidWizardSidParser(),
    player=SWMPlayer,
    describe=_describe,
    metadata=_metadata,
)
