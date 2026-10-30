"""Deterministic seven-phase tick orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from sheltersim.core.clock import ClockExhaustedError, SimulationClock
from sheltersim.core.rng import CounterRNG, KeyedRandom
from sheltersim.core.world import WorldState


class TickPhase(StrEnum):
    """Canonical per-minute simulation phase order."""

    DISPATCH_DUE_EVENTS = "dispatch_due_events"
    COMPLETE_SCHEDULED_TASKS = "complete_scheduled_tasks"
    UPDATE_AGENT_STATE = "update_agent_state"
    PROPOSE_ACTIONS = "propose_actions"
    ARBITRATE_ACTIONS = "arbitrate_actions"
    SAMPLE_ENDOGENOUS_EVENTS = "sample_endogenous_events"
    COLLECT_METRICS = "collect_metrics"


TICK_PHASE_ORDER = (
    TickPhase.DISPATCH_DUE_EVENTS,
    TickPhase.COMPLETE_SCHEDULED_TASKS,
    TickPhase.UPDATE_AGENT_STATE,
    TickPhase.PROPOSE_ACTIONS,
    TickPhase.ARBITRATE_ACTIONS,
    TickPhase.SAMPLE_ENDOGENOUS_EVENTS,
    TickPhase.COLLECT_METRICS,
)


@dataclass(frozen=True, order=True)
class DeterministicOrderKey:
    """Stable arbitration order: minute, priority, sequence, then Agent ID."""

    minute: int
    priority: int
    sequence: int
    agent_id: str


class TraceLevel(StrEnum):
    """Trace detail level; tracing is observational and uses no RNG draws."""

    OFF = "off"
    SUMMARY = "summary"
    FULL = "full"


@dataclass(frozen=True)
class TickTrace:
    """Immutable trace record that contains no mutable simulation references."""

    minute_index: int
    phase: TickPhase | None


PhaseHandler = Callable[[TickPhase, WorldState, int, KeyedRandom], None]
TraceSink = Callable[[TickTrace], None]


class TickExecutionError(RuntimeError):
    """Raised when a phase violates the engine-owned tick clock contract."""


def no_op_phase_handler(
    phase: TickPhase, world: WorldState, minute_index: int, rng: KeyedRandom
) -> None:
    """Default phase callback while domain behavior is introduced by later Issues."""


class TickEngine:
    """Run bounded one-minute ticks in a fixed, inspectable phase order."""

    def __init__(
        self,
        world: WorldState,
        *,
        phase_handler: PhaseHandler = no_op_phase_handler,
        rng: KeyedRandom | None = None,
        trace_level: TraceLevel = TraceLevel.OFF,
        trace_sink: TraceSink | None = None,
    ) -> None:
        level = TraceLevel(trace_level)
        if level is not TraceLevel.OFF and trace_sink is None:
            raise ValueError("a trace sink is required when tracing is enabled")
        self.world = world
        self.clock = SimulationClock(world.duration_minutes, world.minute_index)
        self.phase_handler = phase_handler
        self.rng = rng or CounterRNG(world.seed)
        self.trace_level = level
        self.trace_sink = trace_sink
        self.world.rng_state = dict(self.rng.metadata)
        self.world.validate()

    def step(self) -> WorldState:
        """Execute one finite tick and advance the simulation clock once."""
        if self.clock.complete:
            raise ClockExhaustedError("cannot step a completed simulation")
        if self.world.minute_index != self.clock.minute_index:
            raise TickExecutionError("WorldState clock was changed outside the TickEngine")

        minute = self.clock.minute_index
        for phase in TICK_PHASE_ORDER:
            self.phase_handler(phase, self.world, minute, self.rng)
            if self.world.minute_index != minute:
                raise TickExecutionError("tick phase attempted to change the engine-owned clock")
            if self.trace_level is TraceLevel.FULL:
                self._emit(TickTrace(minute_index=minute, phase=phase))

        self.clock.advance()
        self.world.minute_index = self.clock.minute_index
        self.world.validate_after_tick()
        if self.trace_level is TraceLevel.SUMMARY:
            self._emit(TickTrace(minute_index=minute, phase=None))
        return self.world

    def run(self, steps: int | None = None) -> WorldState:
        """Run up to ``steps`` minutes, or continue until duration is reached."""
        if steps is not None and steps < 0:
            raise ValueError("steps cannot be negative")
        remaining = self.world.duration_minutes - self.clock.minute_index
        count = remaining if steps is None else min(steps, remaining)
        for _ in range(count):
            self.step()
        # Audit append-only EventLog relationships at the run boundary. Per-tick
        # validation checks current state and indexes without rescanning all history.
        self.world.validate()
        return self.world

    def _emit(self, record: TickTrace) -> None:
        if self.trace_sink is None:
            return
        self.trace_sink(record)
