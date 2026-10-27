"""Opt-in measurement adapters outside the deterministic simulation core."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any


class PhaseProfiler:
    """Wrap a phase callback; timekeeping is observational and consumes no RNG."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: dict[str, int] = defaultdict(int)
        self.wall_seconds: dict[str, float] = defaultdict(float)
        self.cpu_seconds: dict[str, float] = defaultdict(float)

    def __call__(self, phase: Any, world: Any, minute: int, rng: Any) -> None:
        wall, cpu = time.perf_counter(), time.process_time()
        try:
            self.handler(phase, world, minute, rng)
        finally:
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
