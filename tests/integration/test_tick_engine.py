"""Deterministic clock, RNG, phase ordering, and trace behavior tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from sheltersim.core.clock import SimulationClock
from sheltersim.core.engine import (
    TICK_PHASE_ORDER,
    DeterministicOrderKey,
    TickEngine,
    TickPhase,
    TickTrace,
    TraceLevel,
)
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.events.handlers import EventDispatcher
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.scenario import ScenarioSpec

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


def test_counter_rng_uses_stable_domain_keys_not_call_order() -> None:
    rng = CounterRNG(2026)
    first_a = rng.random("agent", "A001", "minute", 17)
    first_b = rng.random("agent", "A002", "minute", 17)

    assert first_a == CounterRNG(2026).random("agent", "A001", "minute", 17)
    assert first_b == CounterRNG(2026).random("agent", "A002", "minute", 17)
    assert first_a != first_b
    assert first_a != CounterRNG(2027).random("agent", "A001", "minute", 17)


def test_counter_rng_requires_a_stable_key() -> None:
    rng = CounterRNG(4)

    try:
        rng.random()
    except ValueError as error:
        assert "stable RNG key" in str(error)
    else:
        raise AssertionError("an unkeyed random request should fail")


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


def test_execution_order_uses_agent_id_as_final_tiebreaker() -> None:
    later_id = DeterministicOrderKey(10, 1, 4, "A002")
    earlier_id = DeterministicOrderKey(10, 1, 4, "A001")
    earlier_sequence = DeterministicOrderKey(10, 1, 3, "A099")
    earlier_minute = DeterministicOrderKey(9, 99, 99, "A999")

    assert sorted((later_id, earlier_id, earlier_sequence, earlier_minute)) == [
        earlier_minute,
        earlier_sequence,
        earlier_id,
        later_id,
    ]


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
