"""The per-tick window of a history list holds the last tick and the record before it.

That WorldState.validate checks only that window each tick, and every record at
the end of a Run, is checked in tests/integration/test_per_tick_history_validation.py.
"""

from __future__ import annotations

from sheltersim.core.history import records_since_previous_tick


def _rows(*minutes: object) -> list[object]:
    return [{"minute": minute, "n": index} for index, minute in enumerate(minutes)]


def test_the_window_holds_the_last_tick_and_the_record_before_it() -> None:
    history = _rows(0, 1, 3, 5, 5)

    assert records_since_previous_tick(history, 6) == history[2:]


def test_the_window_keeps_the_last_record_when_the_tick_added_none() -> None:
    history = _rows(0, 1)

    assert records_since_previous_tick(history, 10) == history[1:]
    assert records_since_previous_tick([], 10) == []


def test_malformed_records_do_not_end_the_scan() -> None:
    history = [{"minute": 0}, "not a record", {"minute": None}, {"minute": 2.0}]

    assert records_since_previous_tick(history, 10) == history
