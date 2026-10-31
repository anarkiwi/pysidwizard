"""Write ``tests/_refused.py`` from ``scripts/sw_refusals.py`` output, each song by its issue.

python scripts/gen_refused.py REFUSALS.tsv
"""

import sys
from collections import defaultdict
from pathlib import Path

STRIPPED = {"Kung_Fu_Pico", "Lost_in_Time", "Autumn_Skies", "Oldschool_Heaven"}
SLOWDOWN = {"Immensity_preview_2", "Emomyst"}

HEADER = '''"""HVSC SID-Wizard songs the playspec binder refuses, each with the issue that tracks it.

Written by ``scripts/gen_refused.py`` from ``scripts/sw_refusals.py``.
"""

import pytest

from pysidwizard import SIDFormatError, SWMPlayer

# A refused song raises within this many play calls (a wrapper may reach PLAYER late).
CALLS = 60
# The binder's refusal text for each issue's builds.
REASONS = {
    121: "partly relocated player is not stated",
    122: "no SID-Wizard big effect",
    123: "no SID-Wizard",
    124: "no SID-Wizard",
    125: "multi-SID",
}

'''

FOOTER = '''

def _play(swm) -> None:
    player = SWMPlayer(swm)
    for _ in range(CALLS):
        player.play_frame()


def assert_refused(path: str, swm) -> bool:
    """For a refused HVSC ``path``: each refused song raises its issue's refusal and every
    other subtune plays; True. False for a tune every song of which binds."""
    songs = REFUSED.get(path)
    if songs is None:
        return False
    for n in range(swm.subtune_count):
        if n + 1 in songs:
            with pytest.raises((ValueError, SIDFormatError), match=REASONS[songs[n + 1]]):
                _play(swm.subtune(n))
        else:
            _play(swm.subtune(n))
    return True
'''


def issue(path: str, error: str) -> int:
    """The issue tracking the refusal ``error`` of HVSC ``path``."""
    stem = Path(path).stem
    if "multi-SID" in error or "SIDs" in error:
        return 125
    if stem in STRIPPED:
        return 122
    if stem in SLOWDOWN:
        return 123
    return 121 if "partly relocated" in error else 124


def main(argv=None) -> None:
    """Parse ``argv`` and write the module."""
    rows = Path((argv or sys.argv[1:])[0]).read_text(encoding="utf-8").splitlines()
    refused = defaultdict(dict)
    for row in rows:
        path, song, error = row.split("\t", 2)
        refused[path][int(song)] = issue(path, error)
    body = ["REFUSED = {"]
    body += [f'    "{p}": {dict(sorted(refused[p].items()))},' for p in sorted(refused)]
    body.append("}")
    out = Path(__file__).resolve().parent.parent / "tests" / "_refused.py"
    out.write_text(HEADER + "\n".join(body) + "\n" + FOOTER, encoding="utf-8")


if __name__ == "__main__":
    main()
