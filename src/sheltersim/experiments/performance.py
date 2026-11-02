"""Opt-in measurement adapters outside the deterministic simulation core."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any


class PhaseProfiler:
    """Times each phase through `TickEngine.phase_observer`, which leaves the Rust driver running.

    `engine.phase_observer = profiler.observe`. Timekeeping is observational and consumes no RNG. A
    phase that raises has no end, so it is not counted.
    """

    def __init__(self) -> None:
        self.calls: dict[str, int] = defaultdict(int)
        self.wall_seconds: dict[str, float] = defaultdict(float)
        self.cpu_seconds: dict[str, float] = defaultdict(float)
        self._started = (0.0, 0.0)

    def observe(self, phase: Any, before: bool) -> None:
        if before:
            self._started = (time.perf_counter(), time.process_time())
            return
        wall, cpu = self._started
        key = str(phase)
        self.calls[key] += 1
        self.wall_seconds[key] += time.perf_counter() - wall
        self.cpu_seconds[key] += time.process_time() - cpu

    def report(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "instrumented": True,
            "overhead_included": True,
            "phases": [
                {
                    "phase": key,
                    "calls": self.calls[key],
                    "wall_seconds": self.wall_seconds[key],
                    "cpu_seconds": self.cpu_seconds[key],
                }
                for key in sorted(self.calls, key=lambda k: -self.wall_seconds[k])
            ],
        }
