"""What SaveValidationMemo lets a save's validation skip, from the facts it is given."""

from __future__ import annotations

from types import SimpleNamespace

from sheltersim.core.save_validation import SaveValidationMemo

_PATH = ("namespace", "history")
_HISTORY = list(range(10))


def _world(minute: int = 10, log: object = "log") -> SimpleNamespace:
    return SimpleNamespace(minute_index=minute, event_log=log)


def _memo_after_read(inputs: object = "inputs", *, log: object = "log") -> SaveValidationMemo:
    memo = SaveValidationMemo()
    memo.begin(_world(log=log), {})
    assert memo.unread(_PATH, _HISTORY, inputs) == _HISTORY
    return memo


def test_a_first_validation_reads_everything_whatever_is_called_unchanged() -> None:
    memo = SaveValidationMemo()
    memo.begin(_world(), {_PATH: 10})
    assert memo.unread(_PATH, _HISTORY, "inputs") == _HISTORY
    assert memo.resume(("other", "history"), "inputs") is None


def test_only_the_last_unchanged_entry_and_later_ones_are_read() -> None:
    memo = _memo_after_read()
    memo.begin(_world(), {_PATH: 6})
    assert memo.unread(_PATH, _HISTORY, "inputs") == [5, 6, 7, 8, 9]


def test_nothing_unchanged_reads_everything() -> None:
    memo = _memo_after_read()
    memo.begin(_world(), {_PATH: 0})
    assert memo.unread(_PATH, _HISTORY, "inputs") == _HISTORY


def test_changed_inputs_read_everything_once() -> None:
    memo = _memo_after_read()
    memo.begin(_world(), {_PATH: 10})
    assert memo.unread(_PATH, _HISTORY, "other inputs") == _HISTORY
    memo.begin(_world(), {_PATH: 10})
    assert memo.unread(_PATH, _HISTORY, "other inputs") == [9]


def test_inputs_that_could_not_be_read_never_match() -> None:
    memo = _memo_after_read(None)
    memo.begin(_world(), {_PATH: 10})
    assert memo.unread(_PATH, _HISTORY, None) == _HISTORY


def test_a_replay_resumes_only_at_exactly_the_entries_it_read() -> None:
    state = object()
    for unchanged, expected in ((10, (10, state)), (9, None), (0, None)):
        memo = SaveValidationMemo()
        memo.begin(_world(), {})
        assert memo.resume(_PATH, "inputs") is None
        memo.keep(_PATH, 10, state)
        memo.begin(_world(), {_PATH: unchanged})
        assert memo.resume(_PATH, "inputs") == expected


def test_a_replay_does_not_resume_with_other_inputs() -> None:
    memo = SaveValidationMemo()
    memo.begin(_world(), {})
    memo.resume(_PATH, "inputs")
    memo.keep(_PATH, 10, object())
    memo.begin(_world(), {_PATH: 10})
    assert memo.resume(_PATH, "other inputs") is None


def test_an_earlier_clock_or_another_event_log_drops_everything() -> None:
    for world in (_world(minute=9), _world(log="another log")):
        memo = _memo_after_read()
        memo.keep(_PATH, 10, object())
        memo.begin(world, {_PATH: 10})
        assert memo.unread(_PATH, _HISTORY, "inputs") == _HISTORY
        assert memo.resume(_PATH, "inputs") is None
