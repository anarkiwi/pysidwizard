"""Resolve HVSC corpus tunes from a local tree or the public HVSC mirror.

The corpus tune list (:mod:`tests._hvsc_corpus`) stores **paths only** — the
HVSC tunes are copyright works and are never committed. This helper resolves
each relative path to bytes on disk: a local HVSC ``C64Music`` tree (``$HVSC``)
is preferred, otherwise the tune is downloaded from the public HVSC mirror
(``$HVSC_MIRROR``) into the gitignored ``tests/.tunecache`` (with retries).

Test-only module; the ``pysidwizard`` runtime package does not import it.
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from pathlib import Path

_DEFAULT_HVSC = "/scratch/preframr/hvsc/C64Music"
CACHE = Path(os.environ.get("HVSC_TUNECACHE", str(Path(__file__).resolve().parent / ".tunecache")))
MIRROR = os.environ.get("HVSC_MIRROR", "https://hvsc.brona.dk/HVSC/C64Music").rstrip("/")

# Transient-failure retry policy for mirror fetches (attempts, fixed backoff).
_FETCH_ATTEMPTS = 4
_FETCH_BACKOFF = 2.0


def _is_sid(data: bytes) -> bool:
    return data[:4] in (b"PSID", b"RSID")


def fetch(rel: str, *, force: bool = False) -> Path:
    """Download ``rel`` from the HVSC mirror into the cache; return its path."""
    rel = rel.lstrip("/")
    dest = CACHE / rel
    if dest.exists() and not force:
        return dest
    url = f"{MIRROR}/{urllib.request.quote(rel)}"
    req = urllib.request.Request(url, headers={"User-Agent": "pysidwizard/hvsc-fetch"})
    data = None
    last_exc: Exception | None = None
    for attempt in range(_FETCH_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                data = resp.read()
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # genuinely absent -- do not retry
                raise
            last_exc = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
        if attempt < _FETCH_ATTEMPTS - 1:
            time.sleep(_FETCH_BACKOFF)
    if data is None:
        raise RuntimeError(f"{url}: fetch failed after retries: {last_exc}")
    if not _is_sid(data):
        raise RuntimeError(f"{url}: not a SID file (magic {data[:4]!r})")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + f".tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, dest)  # atomic: concurrent xdist workers cannot tear it
    return dest


def resolve(rel: str) -> Path | None:
    """Path to ``rel`` from a local HVSC tree or the mirror cache, or ``None``.

    Prefers a local HVSC ``C64Music`` tree (``$HVSC``, default
    ``/scratch/preframr/hvsc/C64Music``); otherwise fetches from the mirror.
    Returns ``None`` only when the tune is genuinely unreachable after retries,
    so an individual tune skips cleanly rather than failing offline.
    """
    root = os.environ.get("HVSC", _DEFAULT_HVSC)
    local = Path(root) / rel
    if local.is_file():
        return local
    try:
        return fetch(rel)
    except Exception:  # noqa: BLE001  # unreachable tune -> caller skips it
        return None
