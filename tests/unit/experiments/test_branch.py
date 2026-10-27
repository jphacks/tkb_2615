"""Typed layout Branch commands: route edits and facility relocation are atomic,
reproducible and leave the source Branch unchanged.

Most tests send the commands through the demo service's layout-branch action.
"""

from __future__ import annotations

import pytest

from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchError, RelocateFacilityCommand, _apply_to_world
from sheltersim.web.demo import DemoError, DemoService


def test_route_edit_branches_are_atomic_reproducible_and_leave_source_unchanged(
    small_replay_scenario, route_edit_command
) -> None:
    demo = DemoService(small_replay_scenario())
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    before_hash = demo.branches["baseline"].runtime.world.canonical_json()
    payload = {
        "source_branch_id": "baseline",
        "idempotency_key": "route-edit-1",
        "commands": [route_edit_command()],
    }

    result = demo.dispatch("layout-branch", payload)
    replay = demo.dispatch("layout-branch", payload)
    child = demo.branches[result["branch_id"]].runtime

    assert result["parent_branch_id"] == "baseline"
    assert result["branch_id"] == replay["branch_id"]
    assert replay["idempotent_replay"] is True
    assert demo.branches["baseline"].runtime.world.canonical_json() == before_hash
    assert child.world.zone_edges["corridor_toilet"].travel_minutes == 8
    assert child.world.model_configs["layout_routing"]["max_agents_in_transit_by_edge"] == {
        "corridor_toilet": 1
    }


def test_invalid_route_edit_set_creates_no_partial_branch(
    small_replay_scenario, route_edit_command
) -> None:
    demo = DemoService(small_replay_scenario())
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    before_hash = demo.branches["baseline"].runtime.world.canonical_json()

    with pytest.raises(DemoError, match="unknown zone edge"):
        demo.dispatch(
            "layout-branch",
            {
                "source_branch_id": "baseline",
                "idempotency_key": "route-edit-invalid",
                "commands": [route_edit_command(), route_edit_command(edge_id="missing-edge")],
            },
        )

    assert list(demo.branches) == ["baseline"]
    assert demo.branches["baseline"].runtime.world.canonical_json() == before_hash


def test_route_edit_that_disconnects_a_facility_is_rejected_atomically(
    small_replay_scenario
) -> None:
    demo = DemoService(small_replay_scenario())
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    before_hash = demo.branches["baseline"].runtime.world.canonical_json()

    with pytest.raises(DemoError, match="would be unreachable"):
        demo.dispatch(
            "layout-branch",
            {
                "source_branch_id": "baseline",
                "idempotency_key": "route-edit-disconnect",
                "commands": [
                    {
                        "kind": "set_zone_edge",
                        "edge_id": "corridor_toilet",
                        "travel_minutes": 2,
                        "closed": True,
                        "max_agents_in_transit": None,
                    }
                ],
            },
        )

    assert list(demo.branches) == ["baseline"]
    assert demo.branches["baseline"].runtime.world.canonical_json() == before_hash


def test_facility_relocation_is_a_typed_branch_intervention(small_replay_scenario) -> None:
    demo = DemoService(small_replay_scenario())
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    source = demo.branches["baseline"].runtime
    before_hash = source.world.canonical_json()

    result = demo.dispatch(
        "layout-branch",
        {
            "source_branch_id": "baseline",
            "idempotency_key": "facility-relocation-1",
            "commands": [
                {"kind": "relocate_facility", "facility_id": "toilet_1", "zone_id": "corridor"}
            ],
        },
    )

    child = demo.branches[result["branch_id"]].runtime
    assert source.world.facilities["toilet_1"].zone_id == "toilet"
    assert child.world.facilities["toilet_1"].zone_id == "corridor"
    assert child.command_log[-1]["command"]["kind"] == "relocate_facility"
    assert child.world.canonical_json() != before_hash


def test_facility_relocation_rejects_unknown_zone_without_partial_change(
    small_replay_scenario
) -> None:
    demo = DemoService(small_replay_scenario())
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    before_hash = demo.branches["baseline"].runtime.world.canonical_json()

    with pytest.raises(DemoError, match="unknown facility zone"):
        demo.dispatch(
            "layout-branch",
            {
                "source_branch_id": "baseline",
                "idempotency_key": "facility-relocation-invalid",
                "commands": [
                    {"kind": "relocate_facility", "facility_id": "toilet_1", "zone_id": "missing"}
                ],
            },
        )

    assert list(demo.branches) == ["baseline"]
    assert demo.branches["baseline"].runtime.world.canonical_json() == before_hash


def test_relocate_facility_command_rejects_active_facility_use(small_replay_scenario) -> None:
    world = WorldState.from_scenario(small_replay_scenario())
    world.facilities["toilet_1"].queue_agent_ids.append("A001")

    with pytest.raises(BranchError, match="active users, queues, or tasks"):
        _apply_to_world(world, RelocateFacilityCommand("toilet_1", "corridor"))
