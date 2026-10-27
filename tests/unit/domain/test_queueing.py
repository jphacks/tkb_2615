"""A resource-queue stall record follows the Agent at the head of the queue.

That a generated Scenario with entered stock then runs a whole day is checked in
tests/integration/test_entered_stock.py.
"""

from __future__ import annotations

import pytest

from sheltersim.core.world import WorldState
from sheltersim.domain.queueing import _drop_stale_resource_stall
from sheltersim.domain.resource_service import SERVICE_STATE_KEY
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.experiments.scenario import ScenarioSpec


def _world(scenario: ScenarioSpec) -> WorldState:
    world = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(world)
    return world


def _meal_station(world: WorldState):
    return next(item for item in world.facilities.values() if item.kind == "meal_distribution")


def test_stall_is_kept_while_its_agent_heads_the_queue(generated_scenario) -> None:
    world = _world(generated_scenario)
    facility = _meal_station(world)
    first, second = sorted(world.agents)[:2]
    facility.resource_queue_agent_ids[:] = [first, second]
    stalls = world.model_state[SERVICE_STATE_KEY]["stall_state"]
    stalls[facility.id] = {"agent_id": first, "reason": "staff"}

    _drop_stale_resource_stall(world, facility.id)

    assert stalls[facility.id] == {"agent_id": first, "reason": "staff"}


@pytest.mark.parametrize("remaining", ["reordered", "left", "empty"])
def test_stall_is_dropped_once_its_agent_no_longer_heads_the_queue(
    generated_scenario, remaining: str
) -> None:
    # Need-weighted reordering or a stalled head leaving used to leave the stall
    # pointing at another Agent, and the next state validation stopped the Run.
    world = _world(generated_scenario)
    facility = _meal_station(world)
    first, second = sorted(world.agents)[:2]
    facility.resource_queue_agent_ids[:] = {
        "reordered": [second, first],
        "left": [second],
        "empty": [],
    }[remaining]
    state = world.model_state[SERVICE_STATE_KEY]
    state["stall_state"][facility.id] = {"agent_id": first, "reason": "staff"}
    history = list(state["queue_stall_history"])

    _drop_stale_resource_stall(world, facility.id)

    assert facility.id not in state["stall_state"]
    assert state["queue_stall_history"] == history
