"""Strict xfails from ``pysidtracker.pathdiff``: one case per top-ranked survey fix.

``check_case`` compares the ``sidwizard`` format's render with the sidtrace oracle and
fails with whydiff's explanation until the fix lands (``docs/usage.md#pathdiff-xfails``).
"""

from pathlib import Path

import pytest
from pysidtracker.pathdiff import check_case, xfail_params

MANIFEST = Path(__file__).with_name("pathdiff_xfails.json")


@pytest.mark.oracle
@pytest.mark.parametrize("case", xfail_params(MANIFEST))
def test_pathdiff_case(case):
    """The case's representative tune still diverges from the oracle."""
    check_case(case)
