"""Discrete simulation clock; it does not read wall time."""

from __future__ import annotations

from dataclasses import dataclass


class ClockExhaustedError(RuntimeError):
    """Raised if a caller advances beyond the configured run duration."""


@dataclass
class SimulationClock:
    """One-minute clock whose current value is the next tick to execute."""

    duration_minutes: int
    minute_index: int = 0

    def __post_init__(self) -> None:
        if self.duration_minutes <= 0:
            raise ValueError("duration_minutes must be positive")
        if not 0 <= self.minute_index <= self.duration_minutes:
            raise ValueError("minute_index is outside the simulation duration")

    @property
    def complete(self) -> bool:
        """Whether all configured simulation minutes have run."""
        return self.minute_index >= self.duration_minutes

    def advance(self) -> None:
        """Advance exactly one minute without consulting wall-clock time."""
        if self.complete:
            raise ClockExhaustedError("simulation clock is already complete")
        self.minute_index += 1
