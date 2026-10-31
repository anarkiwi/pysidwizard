"""The tests of a ``pytest -v -n N`` log that have not reported yet: the running ones.

Usage: ``pytest_inflight.py COLLECTED LOG`` where COLLECTED is ``pytest --collect-only -q``
output and LOG the run's ``-v`` output. Prints each in-flight test id and a done/total line.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_RESULT = re.compile(
    r"(?:\[gw\d+\]\s+\[\s*\d+%\]\s+)?(?:PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+)"
)


def inflight(collected: str, log: str) -> tuple[list, int, int]:
    """The ids in ``collected`` with no result in ``log``, and the done and total counts."""
    ids = [line.strip() for line in collected.splitlines() if "::" in line]
    done = {m.group(1) for m in _RESULT.finditer(log)}
    return [i for i in ids if i not in done], len(done & set(ids)), len(ids)


def main(argv: list) -> int:
    """Print the in-flight tests of ``argv[0]`` (collected) against ``argv[1]`` (log)."""
    pending, done, total = inflight(
        Path(argv[0]).read_text(encoding="utf-8"), Path(argv[1]).read_text(encoding="utf-8")
    )
    for test_id in pending:
        print(test_id)
    print(f"{done}/{total} reported, {len(pending)} pending")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
