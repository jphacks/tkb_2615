"""What a kernel's shadow needs: a recorder that survives worker processes, and a pure comparison.

A lane adds benchmarks/rule_shadow/shadow_<name>.py with `prepare(output)`. sitecustomize.py runs
each one before it imports the simulator, so a shadow can replace a kernel in its module before
`core.kernels.kernel_first` looks it up. shadow_staff_dynamics.py is the example.
"""

from __future__ import annotations

import atexit
import json
import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any


def text(value: Any) -> str:
    """Float bits, 1 against 1.0 and the key order all show."""
    return json.dumps(value, ensure_ascii=False)


def values_in(value: Any) -> int:
    """How many leaves a document has, for the count of values compared."""
    if isinstance(value, dict):
        return sum(values_in(item) for item in value.values())
    if isinstance(value, list):
        return sum(values_in(item) for item in value)
    return 1


class Recorder:
    """Counts of one shadow in one process, written to DIR/<pid>-<name>.json at exit."""

    def __init__(self, output: str, name: str) -> None:
        self.name = name
        self.seen: Counter[str] = Counter()
        self.reasons: Counter[str] = Counter()
        self.differences: list[str] = []
        target = Path(output) / f"{os.getpid()}-{name}.json"

        def report() -> None:
            if self.seen or self.reasons or self.differences:
                document = {
                    "seen": {f"{name}.{key}": count for key, count in self.seen.items()},
                    "unsupported": {f"{name}: {key}": n for key, n in self.reasons.items()},
                    "differences": self.differences[:20],
                }
                target.write_text(json.dumps(document), encoding="utf-8")

        atexit.register(report)

    def differ(self, what: str) -> None:
        self.seen["differences"] += 1
        self.differences.append(what)


def pure_shadow(
    recorder: Recorder,
    label: str,
    kernel: Callable[..., Any],
    python_rule: Callable[[], Callable[..., Any]],
    unsupported: type[Exception],
    *,
    detail: Callable[[Any], None] = lambda result: None,
) -> Callable[..., Any]:
    """A stand-in for a kernel that only reads or builds: the same result as the rule, or Unsupported.

    python_rule gives the rule when it is called, since the rule's module imports the stand-in.
    """

    def shadow(*arguments: Any, **options: Any) -> Any:
        recorder.seen[f"{label}.calls"] += 1
        try:
            expected = python_rule()(*arguments, **options)
            error = None
        except Exception as raised:  # noqa: BLE001 - only compared with what the kernel does
            expected = None
            error = raised
        try:
            result = kernel(*arguments, **options)
        except unsupported as reason:
            recorder.reasons[f"{label}: {reason}"] += 1
            raise
        recorder.seen[f"{label}.kernel_ran"] += 1
        if error is not None:
            recorder.differ(f"{label}: the kernel took what Python turned down: {error}")
        elif text(result) != text(expected):
            recorder.differ(
                f"{label}: {arguments!r:.200} gives {result!r:.200}, not {expected!r:.200}"
            )
        else:
            recorder.seen[f"{label}.values_compared"] += values_in(expected)
            detail(result)
        return result

    return shadow
