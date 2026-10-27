"""WorldState.validate rejects records that point at people the Run does not know."""

from __future__ import annotations

import pytest

from sheltersim.core.world import StateInvariantError, WorldState


def _water_request(agent_id: str) -> dict[str, object]:
    return {
        "minute": 0,
        "agent_id": agent_id,
        "action": "fetch_water",
        "outcome": "rejected",
        "reason": "no_facility",
        "facility_id": None,
        "origin_zone_id": "sleeping",
        "detail": None,
    }


@pytest.mark.parametrize(("agent_id", "valid"), [("A001", True), ("A999", False)])
def test_service_history_must_name_a_known_person(generated_scenario, agent_id, valid) -> None:
    # People away on a temporary exit are also known (#79); the intervention
    # test test_someone_who_has_used_water_or_meals_can_go_out_and_come_back runs that.
    world = WorldState.from_scenario(generated_scenario)
    world.model_state["resource_service_v1"]["request_history"].append(_water_request(agent_id))

    if valid:
        world.validate()
    else:
        with pytest.raises(StateInvariantError, match="request history is invalid"):
            world.validate()
