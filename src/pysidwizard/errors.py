"""Exceptions raised by :mod:`pysidwizard`.

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


class SIDFormatError(SWMFormatError):
    """Raised when a ``.sid`` (PSID) file cannot be parsed as a SID-Wizard tune.

    A subclass of :class:`SWMFormatError` so callers that already catch the
    latter keep working. Used for: not a PSID, RSID/multi-SID (deferred to a
    later phase), a missing ``SWM1`` tune header, truncation, or pointer
    tables that do not resolve to a coherent SID-Wizard tune image.
    """
