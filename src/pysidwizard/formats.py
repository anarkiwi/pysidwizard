"""Register SID-Wizard with the generic ``pysidtracker`` command-line tool.

Installing pysidwizard exposes a :class:`~pysidtracker.formats.SidFormat` on the
``pysidtracker.formats`` entry-point group, so ``pysidtracker info|reglog|wav``
work on SID-Wizard ``.sid`` tunes: the format's parser
(:class:`~pysidwizard.sidreader.SidWizardSidParser`) recognises and decodes the
tune, its player (:class:`~pysidwizard.player.SWMPlayer`) plays it, and its
``spec`` hook is the playspec of the tune's own image, which
:meth:`~pysidtracker.formats.SidFormat.spec_player` plays (``notes=clock`` for
standard note words).
"""

from __future__ import annotations

from typing import Iterable, Sequence

from pysidtracker import SidFormat, parse_sid_header

from .grammar import GRAMMAR, streams
from .model import SWMFile
from .player import SWMPlayer, spec_binding, tune_sid
from .sidreader import SidWizardSidParser


def _metadata(swm: SWMFile) -> Sequence:
    """``(name, author, released, load, init, play)`` from the header of the ``.sid`` played."""
    header = parse_sid_header(tune_sid(swm)[0])
    load = header.real_load_address
    return (
        header.name,
        header.author,
        header.released,
        load,
        header.init_address or load,
        header.play_address,
    )


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
    grammar=GRAMMAR,
    streams=streams,
    player_modules=("pysidwizard.playspec", "pysidwizard:specs/sidwizard.play"),
    spec=spec_binding,
)
