"""A temporary exit or transfer interrupts the person's open action with its cause.

A temporary absence also keeps the reference to the state the person returns to.

The exit and return on a running generated Scenario are checked in
tests/integration/test_intervention_outcomes.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from sheltersim.contracts.presence import PresenceRecord
from sheltersim.core.engine import TickPhase, TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.action_lifecycle import record_action_proposal
from sheltersim.events.effects import (
    EffectProgram,
    SupportMode,
    TargetSelector,
    TimedAction,
    schedule_effect_program,
)
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.policies.behavior import ActionKind


@pytest.mark.parametrize(
    ("action", "parameters", "reason_code"),
    [
        ("temporary_exit", {"expected_return_minute": 5}, "population_temporary_exit"),
        ("transfer", {"zone_id": "corridor"}, "population_transfer"),
    ],
)
def test_typed_population_absence_interrupts_open_action(
    generated_scenario, action: str, parameters: dict[str, object], reason_code: str
) -> None:
    world = WorldState.from_scenario(generated_scenario)

    class ArtifactSink:
        trace_level = TraceLevel.OFF

        def record_decision(self, *_args: object, **_kwargs: object) -> None:
            pass

    controller = OfflineSimulationController(world, ArtifactSink())
    record_action_proposal(
        world,
        SimpleNamespace(
            agent_id="A001",
            action=ActionKind.WAIT,
            minute_index=0,
            trigger="typed_population_absence_lifecycle_test",
        ),
        target_id=None,
        committed=True,
        active_actions=controller.active_actions,
    )
    effect = TimedAction(
        effect_id=f"population-{action}",
        op="population_presence",
        target_selector=TargetSelector(kind="person", target_id="A001"),
        start_minute=1,
        parameters={"action": action, **parameters},
        units="population_transition",
        source_id="ASM-TEST-PRESENCE-LIFECYCLE-V1",
        policy_dependencies=["population_lifecycle_v1"],
        reversible=False,
    )
    schedule_effect_program(
        world,
        EffectProgram(
            program_id=f"population-absence-{action}",
            narrative="Apply one typed population absence transition.",
            source_text=action,
            simulation_start_minute=1,
            effects=[effect],
            assumptions=[],
            support_mode=SupportMode.NATIVE,
            compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
        ),
    )
    event = next(
        item
        for item in world.event_queue.pending()
        if item.event_type == "effect_program_population"
        and item.payload.get("op") == "population_presence"
    )

    world.minute_index = 1
    controller(TickPhase.DISPATCH_DUE_EVENTS, world, 1, CounterRNG(world.seed))

    assert world.event_log.find(event.event_id).status.value == "applied"
    assert "A001" not in world.agents
    interruption = next(
        row
        for row in world.event_log.records
        if row.event.event_type == "agent_action_lifecycle"
        and row.event.payload["agent_id"] == "A001"
        and row.event.payload["transition"] == "interrupted"
    )
    assert interruption.event.causation_id == event.event_id
    assert interruption.event.payload["reason_code"] == reason_code


def test_temporary_absence_cannot_drop_the_return_state_reference() -> None:
    with pytest.raises(ValidationError, match="retained state reference"):
        PresenceRecord(agent_id="person", status="temporarily_absent", since_minute=1)
