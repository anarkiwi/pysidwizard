"""The player variant a ``.sid`` export fixes: its build, release quirks and init state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from pysidtracker.facets import DEFAULT_SOURCE, Entry, Source, Variant
from pysidtracker.locate import VARIANT_FACET

from .features import FACETS, PlayerFeatures, PlayerQuirks
from .model import Instrument


@dataclass
class PlayerImage:
    """What a ``.sid``'s player image fixes beyond the module.

    Its code's build flags and release quirks, the tables the assembler placed, and the
    state the tune's init leaves for the first play call. A bare ``.swm`` has none: its
    player takes the driver byte's build and these defaults.
    """

    # The build flags read from the player code (the driver byte's build for a bare ``.swm``).
    features: PlayerFeatures
    quirks: PlayerQuirks = PlayerQuirks()
    # Where each facet read from the image came from (``detected@$addr`` / ``inferred(...)``), keyed by facet name; a facet absent here keeps the driver byte's default.
    provenance: Dict[str, Source] = field(default_factory=dict, compare=False)
    # The ``locate()`` strategy that found the tune's tables (``swm-header``, ``swp``, ``codescan`` or ``via-player``); empty for a bare ``.swm``.
    layout: str = field(default="", compare=False)
    # The image's own FREQTBL / FREQTBH spans (256 bytes each from each table base): PAL or NTSC, plus the bytes past each table.
    freq_lo: bytes = b""
    freq_hi: bytes = b""
    # Image bytes following the player's FREQTBL. Indexed pitch loads past the table's 96 notes read them (player.asm:2523-2535).
    freq_table_tail: bytes = b""
    # SID registers ($D400-$D418) as the ``.sid``'s init left them over the PSID driver's $0F volume; empty is all zero.
    sid_init: bytes = b""
    # ``(RESONIB, CTFHGHO, CTFLGHO, FSWITCH)`` operands as the tune's init leaves them (player.asm:418, 749-797), which the first call writes.
    filter_init: Tuple[int, int, int, int] = (0, 0, 0, 0)
    # What ``lda (PLAYERZP),y`` reads where the init left PLAYERZP ($FE/$FF), which a RETAINZEROPAGE PLAYER restores every call (player.asm:609-613, 888-892).
    zp_init: bytes = b""
    # ``(operand, mask, play_sign)`` of a PLAYER entry that toggles its own ``lda #`` operand with ``eor #mask`` each call and returns unless bit 7 equals ``play_sign``; empty when it always plays.
    play_gate: Tuple[int, ...] = ()
    # The instrument INSPTLO/HI slot 0 points at, when it is not instrument 1 (which the exporter's own link implies).
    dummy_instrument: Optional[Instrument] = None
    # The instruments INSPTLO/HI slots past the count point at, by slot; none means the depacker's blank.
    spare_instruments: Dict[int, Instrument] = field(default_factory=dict)

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
