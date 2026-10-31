"""The SID-Wizard player: ``specs/sidwizard.play`` run on the tune's own 6502 image.

A module read from a ``.sid`` plays that container (:attr:`SWMFile.source`); a bare
module plays the ``.sid`` SID-Maker exports for it (:func:`pysidwizard.sidmaker.export`).
The init runs on the 6502; each play call is the playspec bound by :mod:`.playspec`.
"""

from __future__ import annotations

from functools import lru_cache

from pysidtracker import SidImage, parse_sid_header
from pysidtracker.formats import SpecBinding
from pysidtracker.playspec import SpecPlayer
from pysidtracker.registers import sid_model_for_flags

from . import playspec, sidmaker
from .errors import SWMFormatError
from .model import SWMFile
from .writer import build_swm

_export = lru_cache(maxsize=64)(sidmaker.export)


def tune_sid(swm: SWMFile) -> tuple[bytes, int]:
    """``(sid, song)``: the PSID/RSID ``swm`` plays and the 0-based song its init receives."""
    if swm.source:
        return swm.source, swm.song
    return _export(build_swm(swm), swm.player_version or sidmaker.LATEST), 0


def spec_binding(swm: SWMFile) -> SpecBinding:
    """The :class:`~pysidtracker.formats.SpecBinding` of ``swm``'s own tune image."""
    data, song = tune_sid(swm)
    return SpecBinding(
        playspec.SPEC,
        playspec.binder(),
        SidImage.from_bytes(data).image,
        playspec.entries,
        song,
        parse_sid_header(data).is_psid,
    )


class SWMPlayer(SpecPlayer):
    """Play an :class:`~pysidwizard.model.SWMFile` one play call per frame.

    ``kwargs`` go to :class:`~pysidtracker.playspec.SpecPlayer` (``validate``,
    ``notes``, ...); the container header sets the SID model and the PAL/NTSC flag.
    """

    def __init__(self, swm: SWMFile, **kwargs) -> None:
        if swm.sid_amount != 1:
            raise SWMFormatError(f"{swm.sid_amount}-SID playback is not stated by the playspec")
        self.swm = swm
        self.sid, song = tune_sid(swm)
        header = parse_sid_header(self.sid)
        image = SidImage.from_bytes(self.sid)
        kwargs.setdefault("sid_model", sid_model_for_flags(header.flags))
        kwargs.setdefault("ntsc", header.is_ntsc)
        super().__init__(
            playspec.SPEC,
            image.image,
            image.load,
            header.init_address or image.load,
            header.play_address,
            song,
            bind=playspec.binder(),
            entries=playspec.entries,
            psid=header.is_psid,
            sid=image if header.play_address == 0 else None,
            **kwargs,
        )


__all__ = ["SWMPlayer", "spec_binding", "tune_sid"]
