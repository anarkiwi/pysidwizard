"""Exceptions raised by :mod:`pysidwizard`."""


class SWMError(Exception):
    """Base class for SWM-related errors."""


class SWMFormatError(SWMError, ValueError):
    """Raised when SWM data is malformed or violates documented limits."""
