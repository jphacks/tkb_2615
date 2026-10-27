"""The simulation clock stops at the Scenario's duration."""

from __future__ import annotations

from sheltersim.core.clock import SimulationClock


def test_clock_rejects_advancing_past_duration() -> None:
    clock = SimulationClock(duration_minutes=1)
    clock.advance()

    assert clock.complete

    try:
        clock.advance()
    except RuntimeError as error:
        assert "already complete" in str(error)
    else:
        raise AssertionError("a complete clock should not advance")
