"""Resolve HVSC corpus tunes from a local tree or the public HVSC mirror.

The corpus tune list (:mod:`tests._hvsc_corpus`) stores **paths only** -- the
HVSC tunes are copyright works and are never committed. Each relative path
resolves to a local HVSC ``C64Music`` tree (``$HVSC``, else :data:`HVSC_ROOT`'s
default) or is downloaded from the HVSC mirror (``$HVSC_MIRROR``) into the
shared pysidtracker cache (``.tunecache``, see :func:`pysidtracker.testing.default_cache`).

Test-only module; the ``pysidwizard`` runtime package does not import it.
"""

from __future__ import annotations

import os
from pathlib import Path

from pysidtracker.testing import resolve_tune

HVSC_ROOT = Path(os.environ.get("HVSC", "/scratch/datasets/hvsc/C64Music"))


def resolve(rel: str) -> Path | None:
    """Path to ``rel`` from a local HVSC tree or the shared cache, or ``None``.

    Returns ``None`` only when the tune is genuinely unreachable (offline and
    uncached), so an individual tune skips cleanly.
    """
    rel = rel.lstrip("/")
    local = HVSC_ROOT / rel
    if local.is_file():
        return local
    return resolve_tune(rel)
