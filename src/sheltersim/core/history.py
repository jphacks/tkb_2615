"""Select the history records a per-tick validation must check."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def records_since_previous_tick(history: Sequence[Any], minute_index: int) -> Sequence[Any]:
    """Return the records appended during the tick that just ended.

    Per-tick validation runs after every tick, so records from earlier ticks were
    already checked; the run-boundary validation still replays the whole history.
    The slice also keeps the last earlier record, so ordering is checked across the
    boundary, and malformed records never end the scan.
    """
    start = len(history)
    while start > 0:
        item = history[start - 1]
        minute = item.get("minute") if isinstance(item, dict) else None
        if type(minute) is int and minute < minute_index - 1:
            break
        start -= 1
    return history[max(start - 1, 0) :]
