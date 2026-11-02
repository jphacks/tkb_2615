"""Compare every moved error of the kernels with what the Python rule raises, in any run.

A kernel that raises an error through native/src/core/errors.rs says it in the Python rule's words.
While the Python rules exist this checks that: with this directory first on PYTHONPATH,

    SHELTERSIM_ERROR_COMPARE=DIR PYTHONPATH=benchmarks/rule_shadow:src:native/pylib \\
        python -m pytest -p error_compare -n auto ...
    python benchmarks/rule_shadow/error_compare.py DIR

each entry that falls back to a Python rule (`kernel_first`, `segments.py`, the driver's
`or_rule!`) also runs the rule after a moved error, on the state the kernel left unchanged, and
counts whether it raised the same class and text. The rule then runs where it wouldn't have, so a
test that reads the state after the error may see its writes: only the counts are the result.
docs/rust-port/lanes/l8-texts-rest.md has the numbers.
"""

from __future__ import annotations

import atexit
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

NATIVE: Any = None
TARGET: Path | None = None


def write(scope: str = "") -> None:
    if NATIVE is None or TARGET is None:
        return
    rows = NATIVE.errors_take()
    if not rows:
        return
    with TARGET.open("a", encoding="utf-8") as output:
        for file, line, verdict, rust, python, n in rows:
            row = [scope, file[file.rfind("native/") + 7 :], line, verdict, rust, python, n]
            output.write(json.dumps(row, ensure_ascii=False) + "\n")


def install(output: str) -> None:
    global NATIVE, TARGET
    if os.environ.get("SHELTERSIM_NATIVE", "1") == "0":
        return
    try:
        import sheltersim_native as native
    except ImportError:
        return
    Path(output).mkdir(parents=True, exist_ok=True)
    from sheltersim.core import kernels

    NATIVE = native
    TARGET = Path(output) / f"{os.getpid()}.jsonl"

    def check(python_rule: Any, arguments: tuple, options: dict, error: Exception) -> None:
        native.errors_compare(error, lambda: python_rule(*arguments, **options))

    kernels._error_check = check
    native.errors_switch(True)
    atexit.register(write)


def pytest_runtest_logfinish(nodeid: str, location: Any) -> None:
    """A pytest plugin (-p error_compare): a worker of xdist may not reach atexit."""
    write(nodeid)


def summary(directory: str) -> int:
    verdicts: Counter[str] = Counter()
    sites: dict[tuple[str, int], Counter[str]] = {}
    bad = []
    for path in sorted(Path(directory).glob("*.jsonl")):
        for line in path.read_text("utf-8").splitlines():
            scope, file, number, verdict, rust, python, n = json.loads(line)
            verdicts[verdict] += n
            sites.setdefault((file, number), Counter())[verdict] += n
            if verdict != "same":
                bad.append((scope, file, number, verdict, rust, python, n))
    print(f"comparisons {dict(verdicts)}; sites {len(sites)}")
    for (file, number), counts in sorted(sites.items()):
        print(f"  {file}:{number} {dict(counts)}")
    for row in bad:
        print("NOT THE SAME", *row)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(summary(sys.argv[1]))
