"""Exceptions raised by :mod:`pysidwizard`."""

from pysidtracker import SidError


class SWMError(SidError):
    """Base class for SWM-related errors.

    Subclasses :class:`pysidtracker.SidError` so callers can catch every
    ``py*`` SID parser's errors uniformly.
    """


class SWMFormatError(SWMError, ValueError):
    """Raised when SWM data is malformed or violates documented limits."""


class SIDFormatError(SWMFormatError):
    """Raised when a ``.sid`` (PSID) file cannot be parsed as a SID-Wizard tune.

    A subclass of :class:`SWMFormatError` so callers that already catch the
    latter keep working. Used for: not a PSID, RSID/multi-SID (deferred to a
    later phase), a missing ``SWM1`` tune header, truncation, or pointer
    tables that do not resolve to a coherent SID-Wizard tune image.
    """
