"""Focused checks for the :mod:`pysidtracker` base integration.

Verifies the error tree re-parents onto :class:`pysidtracker.SidError` and that
the :class:`~pysidwizard.SidWizardSidParser` adapter classifies a direct-load
SID-Wizard tune as :attr:`~pysidtracker.PlayroutineKind.DIRECT` via the shared
``detect`` surface.
"""

from __future__ import annotations

import pysidtracker

from pysidwizard import SIDFormatError, SidWizardSidParser, SWMError, SWMFormatError
from tests.test_sidreader import _build_reference_image


def test_error_tree_subclasses_pysidtracker_sid_error():
    assert issubclass(SWMError, pysidtracker.SidError)
    assert issubclass(SWMFormatError, pysidtracker.SidError)
    assert issubclass(SIDFormatError, pysidtracker.SidError)


def test_parser_detects_direct_sidwizard_tune():
    sid, _ = _build_reference_image()
    detection = SidWizardSidParser().detect(sid)
    assert detection.kind is pysidtracker.PlayroutineKind.DIRECT
    assert detection.ran_init is False
