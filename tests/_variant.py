"""Set a test module's player image fields and quirks by name."""

from __future__ import annotations

from dataclasses import fields, replace

from pysidwizard import PlayerImage, PlayerQuirks, SWMFile
from pysidwizard.features import features_for_driver

_QUIRKS = frozenset(f.name for f in fields(PlayerQuirks))


def with_image(swm: SWMFile, **changes) -> SWMFile:
    """``swm`` with its player image (else its driver byte's default) updated by ``changes``."""
    image = swm.player or PlayerImage(features_for_driver(swm.driver_type))
    quirks = {name: changes.pop(name) for name in _QUIRKS & changes.keys()}
    swm.player = replace(image, quirks=replace(image.quirks, **quirks), **changes)
    return swm
