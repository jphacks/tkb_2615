"""A registered model state changes only as a whole, and a Checkpoint refuses a changed version."""

from __future__ import annotations

from pathlib import Path

import pytest

SMALL_REPLAY = Path(__file__).parents[3] / "fixtures" / "scenarios" / "h00_small_replay.json"


def test_registered_state_proposals_are_atomic_and_versions_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copy import deepcopy

    from sheltersim.contracts import runtime as contracts
    from sheltersim.contracts.workspace import StateDescriptor
    from sheltersim.core.checkpoint import Checkpoint, CheckpointError
    from sheltersim.core.world import WorldState
    from sheltersim.experiments.scenario import ScenarioSpec

    namespace = "qa_body_v1"
    descriptor = StateDescriptor(
        namespace=namespace,
        implementation_id="qa-body",
        implementation_version="1",
        schema_version=1,
        fields={"sleep_debt": {"unit": "minute", "public": True}},
    )

    def initial(world: WorldState) -> dict:
        return {"schema_version": 1, "agents": {key: {"sleep_debt": 0} for key in world.agents}}

    def validate(world: WorldState, state: dict) -> None:
        if set(state["agents"]) != set(world.agents) or any(
            row["sleep_debt"] < 0 for row in state["agents"].values()
        ):
            raise ValueError("invalid synthetic debt state")

    def project(state: dict, entity_id: str) -> dict:
        return {"sleep_debt": state["agents"][entity_id]["sleep_debt"]}

    monkeypatch.setitem(contracts._CONTRACTS, namespace, (descriptor, validate, initial, project))
    document = ScenarioSpec.load(SMALL_REPLAY).model_dump(mode="json")
    document["model_configs"]["state_contracts"] = {
        "schema_version": 1,
        "namespaces": {
            namespace: {
                "implementation_id": "qa-body",
                "implementation_version": "1",
                "schema_version": 1,
            }
        },
    }
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    original = deepcopy(world.to_dict())
    assert contracts.entity_projections(world)
    assert world.to_dict() == original
    invalid = deepcopy(world.model_state[namespace])
    invalid["agents"][next(iter(world.agents))]["sleep_debt"] = -1
    with pytest.raises(ValueError):
        contracts.apply_model_proposal(
            world,
            contracts.ModelStateProposal(
                namespace=namespace, model_id="qa-body", minute=0, proposed_state=invalid
            ),
        )
    assert world.to_dict() == original
    checkpoint = Checkpoint.capture(world)
    monkeypatch.setitem(
        contracts._CONTRACTS,
        namespace,
        (descriptor.model_copy(update={"implementation_version": "2"}), validate, initial, project),
    )
    with pytest.raises(CheckpointError, match="migration"):
        checkpoint.restore_world()
