"""Stepping the TickEngine: phase order, validation, determinism and traces.

The RNG, the execution order key and the clock alone are checked in
tests/unit/core/test_rng.py, test_engine.py and test_clock.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sheltersim.core.engine import (
    TICK_PHASE_ORDER,
    TickEngine,
    TickPhase,
    TickTrace,
    TraceLevel,
)
from sheltersim.core.world import WorldState
from sheltersim.events.handlers import EventDispatcher
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.scenario import ScenarioSpec

FIXTURE = Path(__file__).parents[2] / "fixtures" / "scenario_100_agents.json"


def test_tick_runs_phases_in_documented_order_and_advances_one_minute() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    observed: list[TickPhase] = []

    def record_phase(phase, state, minute, rng) -> None:
        observed.append(phase)

    engine = TickEngine(world, phase_handler=record_phase)
    engine.step()

    assert tuple(observed) == TICK_PHASE_ORDER
    assert world.minute_index == 1
    assert engine.clock.minute_index == 1
    assert world.rng_state == {"algorithm": "sha256-keyed-v1", "seed": 2026}


def test_tick_validation_defers_immutable_event_history_audit_to_run_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    engine = TickEngine(world, phase_handler=lambda phase, state, minute, rng: None)
    original_validate = world.event_log.validate
    calls = 0

    def count_history_validation() -> None:
        nonlocal calls
        calls += 1
        original_validate()

    monkeypatch.setattr(world.event_log, "validate", count_history_validation)

    engine.step()
    assert calls == 0

    engine.run(steps=2)
    assert calls == 1
    assert world.minute_index == 3


def test_same_seed_and_phase_inputs_produce_same_canonical_state() -> None:
    scenario = ScenarioSpec.load(FIXTURE)

    def run_once() -> str:
        world = WorldState.from_scenario(scenario)

        def draw_for_agent(phase, state, minute, rng) -> None:
            if phase is TickPhase.PROPOSE_ACTIONS:
                draw = rng.random("behavior", "A001", minute, "toilet")
                state.metric_state["draw_sum"] = state.metric_state.get("draw_sum", 0.0) + draw

        TickEngine(world, phase_handler=draw_for_agent).run(steps=12)
        return world.canonical_json()

    assert run_once() == run_once()


def test_trace_levels_do_not_change_state_or_random_draws() -> None:
    scenario = ScenarioSpec.load(FIXTURE)

    def run_with_trace(level: TraceLevel) -> tuple[str, list[TickTrace]]:
        world = WorldState.from_scenario(scenario)
        records: list[TickTrace] = []

        def sample(phase, state, minute, rng) -> None:
            if phase is TickPhase.UPDATE_AGENT_STATE:
                draw = rng.random("psychology", "A001", minute, "stress")
                state.metric_state["sample_total"] = (
                    state.metric_state.get("sample_total", 0.0) + draw
                )

        engine = TickEngine(
            world,
            phase_handler=sample,
            trace_level=level,
            trace_sink=records.append if level is not TraceLevel.OFF else None,
        )
        engine.run(steps=5)
        return world.canonical_json(), records

    off_state, off_records = run_with_trace(TraceLevel.OFF)
    summary_state, summary_records = run_with_trace(TraceLevel.SUMMARY)
    full_state, full_records = run_with_trace(TraceLevel.FULL)

    assert off_state == summary_state == full_state
    assert off_records == []
    assert len(summary_records) == 5
    assert len(full_records) == 5 * len(TICK_PHASE_ORDER)


def test_same_minute_event_chain_cannot_loop_inside_one_tick() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    dispatched: list[str] = []

    class RecordEvent:
        def apply(self, state, event) -> None:
            dispatched.append(event.event_id)

    dispatcher = EventDispatcher({"record": RecordEvent()})

    def schedule_same_minute_event(phase, state, minute, rng) -> None:
        dispatcher(phase, state, minute, rng)
        if phase is TickPhase.SAMPLE_ENDOGENOUS_EVENTS:
            sequence = state.allocate_event_sequence()
            state.schedule_event(
                ScheduledEvent(
                    event_id=f"E{minute}",
                    kind=EventKind.ENDOGENOUS,
                    due_minute=minute,
                    priority=0,
                    sequence=sequence,
                    event_type="record",
                )
            )

    engine = TickEngine(world, phase_handler=schedule_same_minute_event)
    engine.step()

    assert dispatched == []
    assert world.minute_index == 1
    assert len(world.event_queue) == 1

    engine.step()

    assert dispatched == ["E0"]
    assert len(world.event_queue) == 1
    assert world.event_queue.pending()[0].event_id == "E1"
    assert [record.event.event_id for record in world.event_log.records] == ["E0"]


@pytest.mark.long_simulation
def test_10080_offline_ticks_complete_without_wall_clock() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    engine = TickEngine(world)

    result = engine.run(steps=10080)

    assert result.minute_index == 10080
    assert engine.clock.complete
