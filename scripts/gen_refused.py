"""Write ``tests/_refused_songs.py`` from ``scripts/sw_refusals.py`` output, each song by its issue.

python scripts/gen_refused.py REFUSALS.tsv
"""

import sys
from collections import defaultdict
from pathlib import Path

STRIPPED = {"Kung_Fu_Pico", "Lost_in_Time", "Autumn_Skies", "Oldschool_Heaven"}
SLOWDOWN = {"Immensity_preview_2", "Emomyst"}

HEADER = '''"""The HVSC SID-Wizard songs the binder refuses, each with the issue tracking it.

Written by ``scripts/gen_refused.py`` from ``scripts/sw_refusals.py``.
"""

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
    out = Path(__file__).resolve().parent.parent / "tests" / "_refused_songs.py"
    out.write_text(HEADER + "\n".join(body) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
