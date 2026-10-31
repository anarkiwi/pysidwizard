"""The player variant a ``.sid`` export fixes: its build and release quirks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from pysidtracker.facets import DEFAULT_SOURCE, Entry, Source, Variant
from pysidtracker.locate import VARIANT_FACET

from .features import FACETS, PlayerFeatures, PlayerQuirks


@dataclass
class PlayerImage:
    """What a ``.sid``'s player image fixes beyond the module: build flags and release quirks.

    A bare ``.swm`` has none: its player takes the driver byte's build.
    """

    # The build flags read from the player code (the driver byte's build for a bare ``.swm``).
    features: PlayerFeatures
    quirks: PlayerQuirks = PlayerQuirks()
    # Where each facet read from the image came from (``detected@$addr`` / ``inferred(...)``), keyed by facet name; a facet absent here keeps the driver byte's default.
    provenance: Dict[str, Source] = field(default_factory=dict, compare=False)
    # The ``locate()`` strategy that found the tune's tables (``swm-header``, ``swp``, ``codescan`` or ``via-player``); empty for a bare ``.swm``.
    layout: str = field(default="", compare=False)

    def variant(self) -> Variant:
        """Every facet's value with its provenance, plus the layout strategy: the full variant."""
        entries = {}
        for facet in FACETS:
            owner = self.quirks if facet.kind == "release" else self.features
            source = self.provenance.get(facet.name, DEFAULT_SOURCE)
            entries[facet.name] = Entry(getattr(owner, facet.name), source)
        if self.layout:
            entries[VARIANT_FACET] = Entry(
                self.layout, self.provenance.get(VARIANT_FACET, DEFAULT_SOURCE)
            )
        return Variant(entries, self.features.name)
