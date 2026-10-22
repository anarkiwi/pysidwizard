"""whydiff player hook for SID-Wizard tunes: ``pysidwizard_player:make(data, song)``."""

from pysidwizard import SWMPlayer, parse_sid_song


def make(data: bytes, song: int) -> SWMPlayer:
    return SWMPlayer(parse_sid_song(data, song))
