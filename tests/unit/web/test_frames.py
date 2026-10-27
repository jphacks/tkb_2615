"""Frames show a generated Scenario's people without changing the world."""

from __future__ import annotations

from copy import deepcopy

import pytest

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.inspection.frames import world_frame


def test_generated_people_context_and_body_survive_frames_and_checkpoints(
    generated_scenario,
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    before = deepcopy(world.to_dict())

    frame = world_frame(world, run_id="run", branch_id="main", computed_until=0)

    assert all(person["body"] is not None for person in frame["people"])
    assert all(
        person["context"]["household_id"] == person["group_id"] for person in frame["people"]
    )
    assert world.to_dict() == before
    restored = Checkpoint.capture(world).restore_world()
    assert restored.to_dict() == before
    del restored.model_state["body_state_v1"]
    with pytest.raises(StateInvariantError, match="invalid simulator extension state"):
        restored.validate()
