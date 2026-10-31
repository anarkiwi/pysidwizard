"""Typed column values and instrument-table rows, as the song grammar builds them.

A :class:`Column` is a pattern or table column byte whose class says what the player
does with it; it equals its byte, so typed rows compare and encode as plain ones. The
table rows, :class:`Chord` and :class:`TempoProgram` each encode back to their bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple, Union

from .constants import (
    CHORD_LOOP,
    CHORD_RETURN,
    TABLE_END,
    TABLE_JUMP,
    TABLE_SET_FLAG,
    TEMPO_PROGRAM_LAST,
)


def signed_byte(byte: int) -> int:
    """``byte`` as a two's-complement value."""
    return byte - 0x100 if byte & 0x80 else byte


class Column(int):
    """A column byte typed by what the player does with it; it equals its byte."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(0x{int(self):02x})"


class Note(Column):
    """Note column ``$01..$5F``: a pitch, the octave shift and transpose added (CLEGATO)."""


class VibratoAmplitude(Column):
    """Note column ``$60..$77``: the instrument's vibrato with this amplitude (VIBAMFX)."""

    @property
    def amplitude(self) -> int:
        """The amplitude nibble."""
        return self & 0x0F


class NoteEffect(Column):
    """Note column ``$78..$7F``, dispatched through NOTEFXTBL; ``$7F`` takes its zero
    offset, the code right after INDEXJ1 (NPORTAM, or NSYNCON without PORTAME_NOTEFX)."""


class Portamento(NoteEffect):
    """``$78`` (PORTAMFX): a tone portamento at DEFAULTPORTA speed (PORTAME_NOTEFX builds)."""


class SyncOn(NoteEffect):
    """``$79`` (SYNCONFX): the waveform's sync bit set."""


class SyncOff(NoteEffect):
    """``$7A`` (SYNCOFFX): the waveform's sync bit cleared."""


class RingOn(NoteEffect):
    """``$7B`` (RINGONFX): the waveform's ring-modulation bit set."""


class RingOff(NoteEffect):
    """``$7C`` (RINGOFFX): the waveform's ring-modulation bit cleared."""


class GateOn(NoteEffect):
    """``$7D`` (GATEONFX): the gate set without restarting the sound."""


class GateOff(NoteEffect):
    """``$7E`` (GATEOFFX): the gate cleared, or the instrument's gate-off table positions."""


class SelectInstrument(Column):
    """Instrument column ``$01..$3E``: the voice's instrument (SEL_INS, below the build's
    instrument count)."""


class Legato(Column):
    """Instrument column ``$3F``: the note's pitch is set without restarting the sound."""


class SmallEffect(Column):
    """A one-byte effect (SMALPFX): instrument column ``$40..$7F``, effect column
    ``$20..$FF``; SMALLFXTBL dispatches on the high nibble with the low nibble as value."""

    @property
    def effect(self) -> int:
        """The effect number, the high nibble."""
        return self >> 4

    @property
    def value(self) -> int:
        """The effect's value, the low nibble."""
        return self & 0x0F


class BigEffect(Column):
    """Effect column ``$01..$1F`` (BIGPTFX): the effect number; the row's ``fx_value``
    is its value."""


class RelativePitch(Column):
    """WF table pitch ``$00..$7E`` / ``$E0..$FF``: semitones from the note (RELPTCH)."""

    @property
    def semitones(self) -> int:
        """The signed offset."""
        return signed_byte(self)


class ChordPitch(Column):
    """WF table pitch ``$7F``: the next interval of the chord (CHORDPOS)."""


class KeepPitch(Column):
    """WF table pitch ``$80``: the pitch is left as it is (WFARP_NOP_SUPP)."""


class AbsolutePitch(Column):
    """WF table pitch ``$81..$DF``: a fixed note (ABSPTCH)."""

    @property
    def note(self) -> int:
        """The note, bit 7 cleared."""
        return self & 0x7F


@dataclass(frozen=True)
class WaveRow:
    """WF table ``$10..$FD``: the waveform register (gated by PTNGATE), then the pitch
    and detune columns."""

    waveform: int
    pitch: Column
    detune: Optional[int] = None

    def encode(self) -> bytes:
        """The bytes the player reads."""
        return bytes([self.waveform, self.pitch, _detune_byte(self.detune)])


@dataclass(frozen=True)
class RepeatRow:
    """WF table ``$00..$0F``: the row's pitch and detune, then ``count`` more frames before
    the next row (ARPSCNT); the waveform is left as it is."""

    count: int
    pitch: Column
    detune: Optional[int] = None

    def encode(self) -> bytes:
        """The bytes the player reads."""
        return bytes([self.count, self.pitch, _detune_byte(self.detune)])


def _detune_byte(detune: Optional[int]) -> int:
    return TABLE_END if detune is None else detune


@dataclass(frozen=True)
class PulseSweep:
    """PW table ``$00..$7F``: the pulse width moves by the signed ``step`` each frame for
    ``frames`` frames."""

    frames: int
    step: int
    keytrack: int = 0

    def encode(self) -> bytes:
        """The bytes the player reads."""
        return bytes([self.frames, self.step & 0xFF, self.keytrack])


@dataclass(frozen=True)
class PulseSet:
    """PW table ``$80..$FD``: PWHIGHO (the byte without bit 7) and PWLOGHO set."""

    high: int
    low: int
    keytrack: int = 0

    def encode(self) -> bytes:
        """The bytes the player reads."""
        return bytes([TABLE_SET_FLAG | self.high, self.low, self.keytrack])


@dataclass(frozen=True)
class FilterSweep:
    """Filter table ``$00..$7F``: the cutoff moves by the signed ``step`` each frame for
    ``frames`` frames."""

    frames: int
    step: int
    keytrack: int = 0

    def encode(self) -> bytes:
        """The bytes the player reads."""
        return bytes([self.frames, self.step & 0xFF, self.keytrack])


@dataclass(frozen=True)
class FilterSet:
    """Filter table ``$80..$FD``: FLTBAND (``band``, bits 4-6), the resonance nibble and
    the cutoff high byte set."""

    band: int
    resonance: int
    cutoff: int
    keytrack: int = 0

    def encode(self) -> bytes:
        """The bytes the player reads."""
        return bytes([TABLE_SET_FLAG | self.band | self.resonance, self.cutoff, self.keytrack])


@dataclass(frozen=True)
class TableJump:
    """Any table's ``$FE`` row: the cursor moves to ``target``, an offset from the
    instrument's base; a WF table target with bit 7 set holds the cursor."""

    target: int
    pad: int = 0

    def encode(self) -> bytes:
        """The bytes the player reads."""
        return bytes([TABLE_JUMP, self.target, self.pad])


TableRow = Union[WaveRow, RepeatRow, PulseSweep, PulseSet, FilterSweep, FilterSet, TableJump]


@dataclass(frozen=True)
class Chord:
    """A chord: signed semitone intervals the WF table's ``$7F`` pitch steps through,
    then ``$7E`` (back to the WF table) or, with ``loop``, ``$7F`` (from its start)."""

    intervals: Tuple[int, ...]
    loop: bool = False

    def encode(self) -> bytes:
        """The bytes the player reads."""
        return bytes(
            [i & 0xFF for i in self.intervals] + [CHORD_LOOP if self.loop else CHORD_RETURN]
        )


@dataclass(frozen=True)
class TempoProgram:
    """A tempo program: each row's tick count in turn; after the last the program
    restarts."""

    tempos: Tuple[int, ...]

    def encode(self) -> bytes:
        """The bytes the player reads."""
        return bytes(self.tempos[:-1]) + bytes([TEMPO_PROGRAM_LAST | self.tempos[-1]])
