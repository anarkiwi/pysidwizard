"""Resolve HVSC corpus tunes from a local tree or the public HVSC mirror.

The corpus tune list (:mod:`tests._hvsc_corpus`) stores **paths only** — the
HVSC tunes are copyright works and are never committed. This helper resolves
each relative path to bytes on disk: a local HVSC ``C64Music`` tree (``$HVSC``)
is preferred, otherwise the tune is downloaded from the public HVSC mirror
(``$HVSC_MIRROR``) into the gitignored ``tests/.tunecache``.

The fetch / resolve / atomic-write / retry core is the shared
:mod:`pysidtracker.testing` implementation (``fetch_tune`` writes atomically via
a temp file + ``os.replace``, so concurrent xdist workers cannot tear the
cache). This module only pins the pysidwizard cache location and the local HVSC root
(``$HVSC``, else :data:`HVSC_ROOT`'s default).

Test-only module; the ``pysidwizard`` runtime package does not import it.
"""

from __future__ import annotations

import os
from pathlib import Path

from pysidtracker.testing import DEFAULT_MIRROR, fetch_tune, resolve_tune

HVSC_ROOT = Path(os.environ.get("HVSC", "/scratch/datasets/hvsc/C64Music"))
CACHE = Path(os.environ.get("HVSC_TUNECACHE", str(Path(__file__).resolve().parent / ".tunecache")))
MIRROR = os.environ.get("HVSC_MIRROR", DEFAULT_MIRROR).rstrip("/")


def fetch(rel: str, *, force: bool = False) -> Path:
    """Download ``rel`` from the HVSC mirror into the cache; return its path."""
    return fetch_tune(rel, cache_dir=CACHE, mirror=MIRROR, force=force)


def resolve(rel: str) -> Path | None:
    """Path to ``rel`` from a local HVSC tree or the mirror cache, or ``None``.

    Prefers the local HVSC ``C64Music`` tree (:data:`HVSC_ROOT`); otherwise the shared resolver checks
    the gitignored cache and finally fetches from the mirror. Returns ``None``
    only when the tune is genuinely unreachable (offline and uncached), so an
    individual tune skips cleanly.
    """
    rel = rel.lstrip("/")
    local = HVSC_ROOT / rel
    if local.is_file():
        return local
    return resolve_tune(rel, cache_dir=CACHE, local_env="HVSC")
