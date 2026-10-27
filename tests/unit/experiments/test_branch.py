"""Typed layout Branch commands: route edits and facility relocation are atomic,
reproducible and leave the source Branch unchanged.

Most of these send the commands through the demo service's layout-branch action.
Typed interventions, their validation and the Branch lineage saved in a Checkpoint
are checked on the 100-person baseline. A Branch from an invalid or tampered approved
plan is refused without changing the source.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from sheltersim.core.world import WorldState
from sheltersim.events.effects import GuardExpression
from sheltersim.experiments.branch import (
    AddStaffCommand,
    AddToiletCommand,
    BranchError,
    BranchRuntime,
    RelocateFacilityCommand,
    SetFacilityWorkingUnitsCommand,
    _apply_to_world,
    command_from_dict,
)
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.schemas import ApprovedPlan, DraftPlan
from sheltersim.web.demo import DemoError, DemoService

BASELINE_100_7D = Path(__file__).parents[3] / "fixtures" / "scenarios" / "baseline_100_7d.json"


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
    small_replay_scenario,
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
    small_replay_scenario,
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


def test_typed_interventions_are_logged_and_do_not_leak_between_branches() -> None:
    parent = BranchRuntime.from_world(WorldState.from_scenario(ScenarioSpec.load(BASELINE_100_7D)))
    with_staff = parent.fork("with-staff")
    with_toilet = parent.fork("with-toilet")

    staff_receipt = with_staff.apply(AddStaffCommand("staff-extra", "sleeping"))
    toilet_receipt = with_toilet.apply(
        AddToiletCommand("toilet-extra", "toilet", capacity=2, working_units=1)
    )
    disable_receipt = with_toilet.apply(
        SetFacilityWorkingUnitsCommand("toilet-extra", working_units=0)
    )

    assert "staff-extra" in with_staff.world.staff
    assert "staff-extra" not in with_toilet.world.staff
    assert "staff-extra" not in parent.world.staff
    assert with_toilet.world.facilities["toilet-extra"].working_units == 0
    assert len(with_staff.world.event_log.records) == 1
    assert len(with_toilet.world.event_log.records) == 2
    assert staff_receipt.state_hash != toilet_receipt.state_hash
    assert toilet_receipt.event_id != disable_receipt.event_id
    assert all(
        record.event.kind.value == "intervention" and record.status.value == "applied"
        for record in (*with_staff.world.event_log.records, *with_toilet.world.event_log.records)
    )


def test_rejected_intervention_is_atomic_and_command_parser_is_strict() -> None:
    runtime = BranchRuntime.from_world(WorldState.from_scenario(ScenarioSpec.load(BASELINE_100_7D)))
    original = runtime.world.canonical_json()

    with pytest.raises(BranchError, match="already exists"):
        runtime.apply(AddStaffCommand("S01", "sleeping"))
    with pytest.raises(BranchError, match="unknown facility"):
        runtime.apply(SetFacilityWorkingUnitsCommand("missing", 0))
    with pytest.raises(BranchError, match="unsupported"):
        command_from_dict({"kind": "add_staff", "staff_id": "x", "zone_id": "sleeping", "extra": 1})

    assert runtime.world.canonical_json() == original
    assert runtime.command_log == []


def test_branch_checkpoint_restores_lineage_and_interventions() -> None:
    root = BranchRuntime.from_world(WorldState.from_scenario(ScenarioSpec.load(BASELINE_100_7D)))
    child = root.fork("staff-branch")
    child.apply(AddStaffCommand("staff-extra", "sleeping"))

    restored = BranchRuntime.from_checkpoint(child.checkpoint(), branch_id="restored")

    assert restored.parent_branch_id == "staff-branch"
    assert restored.pairing_key == child.pairing_key
    assert restored.world.canonical_json() == child.world.canonical_json()
    assert restored.command_log == child.command_log


def _approved(plan: DraftPlan) -> dict[str, Any]:
    return ApprovedPlan.from_explicit_confirmation(plan).model_dump(mode="json")


def _world_hash(demo: DemoService, branch_id: str = "baseline") -> str:
    canonical = demo.branches[branch_id].runtime.world.canonical_json()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_invalid_approved_program_is_atomic_and_tampered_approval_is_rejected(
    small_replay_demo, day4_plan
) -> None:
    demo = small_replay_demo(duration_minutes=20)
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    source_hash = _world_hash(demo)
    source_command_log = list(demo.branches["baseline"].runtime.command_log)

    invalid_plan = day4_plan(fork_minute=0)
    invalid_last_action = invalid_plan.effects[2].model_copy(
        update={"guard": GuardExpression(operator="gte", field="target.working_units", value=0)}
    )
    invalid_plan = invalid_plan.model_copy(
        update={"effects": [*invalid_plan.effects[:2], invalid_last_action]}
    )
    with pytest.raises(DemoError, match="guard field"):
        demo.dispatch(
            "branch-plan",
            {
                "source_branch_id": "baseline",
                "approved_plan": _approved(invalid_plan),
                "idempotency_key": "invalid-composite-01",
            },
        )
    assert set(demo.branches) == {"baseline"}
    assert _world_hash(demo) == source_hash
    assert demo.branches["baseline"].runtime.command_log == source_command_log
    assert demo.operations[-1]["accepted"] is False

    tampered = _approved(day4_plan(fork_minute=0))
    tampered["approval_hash"] = "0" * 64
    with pytest.raises(DemoError, match="approval hash does not match"):
        demo.dispatch(
            "branch-plan",
            {
                "source_branch_id": "baseline",
                "approved_plan": tampered,
                "idempotency_key": "tampered-approval-01",
            },
        )
    assert set(demo.branches) == {"baseline"}
    assert _world_hash(demo) == source_hash
