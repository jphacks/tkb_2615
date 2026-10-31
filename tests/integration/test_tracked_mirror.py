"""A tick on the test-only tracked mirror matches the plain tick, phase by phase.

Rust will own model_state and unported Python will write to it through tracked containers
(docs/rust-port/lanes/p1-tracked-mirror.md). benchmarks/tracked_mirror_gate.py runs this over many
scenarios; here it runs over the common generated Scenario, and the harness itself must name the
minute, phase and namespace of a write that goes astray.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from support.tracked_mirror import Drift, Session, TrackedDict, TrackedList, mirror_of

from sheltersim.core.engine import TickEngine, TickPhase
from sheltersim.core.world import WorldState
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink

MINUTES = 40


def _run(scenario: Any, session: Session) -> dict[str, Any]:
    with session:
        world = WorldState.from_scenario(scenario)
        metrics = MetricsRecorder()
        controller = OfflineSimulationController(world, _MetricSink(metrics))
        TickEngine(world, phase_handler=controller).run(steps=MINUTES)
        return world.to_dict()


def test_tracked_and_reversed_runs_match_the_plain_run_at_every_phase(generated_scenario) -> None:
    plain = Session(tracked=False, record=True)
    expected = _run(generated_scenario, plain)

    for options in ({}, {"reverse_at_phase_end": True}, {"reverse_reads": True}):
        session = Session(expect=plain.stream, **options)
        assert _run(generated_scenario, session) == expected
        assert session.drifts == []
        assert session.compared_phases == MINUTES * len(TickPhase)
        assert session.compared_shadow == MINUTES * len(TickPhase)


def test_a_world_keeps_tracked_documents_through_copy_and_swap(generated_scenario) -> None:
    session = Session()
    with session:
        world = WorldState.from_scenario(generated_scenario)
        candidate = WorldState.from_dict(world.to_dict())
        assert isinstance(candidate.model_state, TrackedDict)
        candidate.model_state["probe"] = {"values": []}
        candidate.model_state["probe"]["values"].append(1)
        world.__dict__.clear()
        world.__dict__.update(candidate.__dict__)

        # The swap brought the candidate's tree, with the shadow its writes built.
        assert isinstance(world.model_state["probe"]["values"], TrackedList)
        assert mirror_of(world.model_state).shadow["probe"] == {"values": [1]}
        assert mirror_of(world.model_state).difference() is None


def _engine_with(scenario: Any, write: Callable[[WorldState], None]) -> TickEngine:
    def handler(phase: TickPhase, world: WorldState, minute: int, _rng: Any) -> None:
        if minute == 3 and phase is TickPhase.UPDATE_AGENT_STATE:
            write(world)
        # Taken out again before the end of the tick, which validates the namespaces.
        if phase is TickPhase.COLLECT_METRICS:
            world.model_state.pop("probe", None)

    return TickEngine(WorldState.from_scenario(scenario), phase_handler=handler)


def _alias_write(world: WorldState) -> None:
    record = world.model_state["probe"] = {"count": 0}
    record["count"] = 1


def test_a_write_through_a_plain_alias_is_named_by_minute_phase_and_namespace(
    generated_scenario,
) -> None:
    plain = Session(tracked=False, record=True)
    with plain:
        _engine_with(generated_scenario, _alias_write).run(steps=5)

    session = Session(expect=plain.stream)
    with session, pytest.raises(Drift, match="minute 3 phase update_agent_state model_state/probe"):
        _engine_with(generated_scenario, _alias_write).run(steps=5)

    assert any(
        "test_tracked_mirror.py" in site
        for site in session.findings["plain_alias_changed_after_insert"]
    )


def test_a_write_past_the_mutators_is_named_by_minute_phase_and_namespace(
    generated_scenario,
) -> None:
    session = Session()
    with session, pytest.raises(Drift, match="minute 3 phase update_agent_state model_state"):
        _engine_with(
            generated_scenario, lambda world: dict.__setitem__(world.model_state, "probe", 1)
        ).run(steps=5)
