"""固定順序でtickを進める最小エンジン。"""

from collections.abc import Callable
from enum import StrEnum

from sheltersim_foundation.core.clock import ClockExhaustedError, SimulationClock
from sheltersim_foundation.core.rng import CounterRNG
from sheltersim_foundation.core.world import WorldState


class TickPhase(StrEnum):
    DISPATCH_DUE_EVENTS = "dispatch_due_events"
    COMPLETE_SCHEDULED_TASKS = "complete_scheduled_tasks"
    UPDATE_AGENT_STATE = "update_agent_state"
    PROPOSE_ACTIONS = "propose_actions"
    ARBITRATE_ACTIONS = "arbitrate_actions"
    SAMPLE_ENDOGENOUS_EVENTS = "sample_endogenous_events"
    COLLECT_METRICS = "collect_metrics"


TICK_PHASE_ORDER = tuple(TickPhase)
PhaseHandler = Callable[[TickPhase, WorldState, int, CounterRNG], None]


def _no_op(phase: TickPhase, world: WorldState, minute: int, rng: CounterRNG) -> None:
    del phase, world, minute, rng


class TickEngine:
    """domain handlerを注入し、1分ずつ固定phase順で実行します。"""

    def __init__(self, world: WorldState, phase_handler: PhaseHandler = _no_op) -> None:
        world.validate()
        self.world = world
        self.clock = SimulationClock(world.duration_minutes, world.minute_index)
        self.rng = CounterRNG(world.seed)
        self.phase_handler = phase_handler
        self.world.rng_state = self.rng.metadata

    def step(self) -> WorldState:
        if self.clock.complete:
            raise ClockExhaustedError("cannot step a completed simulation")
        minute = self.clock.minute_index
        for phase in TICK_PHASE_ORDER:
            self.phase_handler(phase, self.world, minute, self.rng)
            if self.world.minute_index != minute:
                raise RuntimeError("phase handler changed the engine-owned clock")
        self.clock.advance()
        self.world.minute_index = self.clock.minute_index
        self.world.validate()
        return self.world

    def run(self, steps: int | None = None) -> WorldState:
        if steps is not None and steps < 0:
            raise ValueError("steps cannot be negative")
        remaining = self.world.duration_minutes - self.world.minute_index
        count = remaining if steps is None else min(steps, remaining)
        for _ in range(count):
            self.step()
        return self.world
