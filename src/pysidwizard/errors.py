"""Exceptions (and the one playback warning) raised by :mod:`pysidwizard`.

The SWM error triple is built by :func:`pysidtracker.make_package_errors`, so
``SWMError`` / ``SWMParseError`` / ``SWMFormatError`` subclass both the package
root and the matching base :class:`pysidtracker.SidParseError` /
:class:`pysidtracker.SidFormatError` -- a base ``except SidFormatError`` catches
this package's own-named format errors. ``SWMFormatError`` additionally mixes in
``ValueError`` for callers that catch that.
"""

from pysidtracker import make_package_errors

SWMError, SWMParseError, _SWMFormatError = make_package_errors("SWM")


class SWMFormatError(_SWMFormatError, ValueError):
    """Raised when SWM data is malformed or violates documented limits."""


class SWMUnsupportedEffectWarning(UserWarning):
    """A tune used an effect :class:`pysidwizard.SWMPlayer` does not model.

    The playback-time analogue of the parse-time scope errors here: the reader
    *raises* on constructs it cannot represent, but a player cannot abort a
    render mid-tune, so an unmodelled effect warns instead. A caller that wants
    a hard failure promotes it::

        warnings.simplefilter("error", SWMUnsupportedEffectWarning)

    Warned once per distinct ``(column, code)`` per player instance; the full
    tally stays available as
    :attr:`~pysidwizard.player.SWMPlayer.unsupported_effects`.

    Attributes:
        column: ``"big-fx"`` or ``"small-fx"`` -- which pattern
            column the code came from.
        code: The effect byte, as stored in the pattern.
        frame: Player frame index the effect was reached on.
        voice: Voice (SID channel) index 0..2.
    """

    def __init__(self, column: str, code: int, frame: int, voice: int) -> None:
        super().__init__(
            f"{column} ${code:02X} on voice {voice} at frame {frame} "
            f"is not implemented by SWMPlayer"
        )
        self.column = column
        self.code = code
        self.frame = frame
        self.voice = voice


class SIDFormatError(SWMFormatError):
    """Raised when a ``.sid`` (PSID) file cannot be parsed as a SID-Wizard tune.

    A subclass of :class:`SWMFormatError` so callers that already catch the
    latter keep working. Used for: not a PSID, RSID/multi-SID (deferred to a
    later phase), a missing ``SWM1`` tune header, truncation, or pointer
    tables that do not resolve to a coherent SID-Wizard tune image.
    """
