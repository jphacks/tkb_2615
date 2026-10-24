"""Approved EffectPrograms fork from checkpoints without mutating their source."""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from typing import Any, get_args

import pytest

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.events.effects import GuardExpression, SupportMode, TargetSelector, TimedAction
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.schemas import ApprovedPlan, DraftPlan
from sheltersim.web.demo import DemoError, DemoService
from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _handler_for

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenarios" / "h00_small_replay.json"
_DAY4_1400 = 3 * 1440 + 14 * 60


def _demo(duration_minutes: int = 5200) -> DemoService:
    document = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    # The ScenarioSpec schema evolves independently from this compact regression fixture.
    document["schema_version"] = get_args(ScenarioSpec.model_fields["schema_version"].annotation)[0]
    document["duration_minutes"] = duration_minutes
    document["scheduled_events"] = []
    return DemoService(ScenarioSpec.model_validate(document))


def _advance_and_pause(demo: DemoService, minute: int) -> None:
    remaining = minute - demo.branches["baseline"].runtime.world.minute_index
    if remaining < 0:
        raise ValueError("target minute cannot be before the current branch minute")
    while remaining:
        branch = demo.branches["baseline"]
        if branch.status == "paused":
            demo.dispatch("resume", {"branch_id": "baseline"})
        steps = min(3000, remaining)
        demo.dispatch("advance", {"branch_id": "baseline", "steps": steps})
        remaining -= steps
        if remaining:
            demo.dispatch("pause", {"branch_id": "baseline"})
    if demo.branches["baseline"].status == "running":
        demo.dispatch("pause", {"branch_id": "baseline"})


def _timed_action(
    *,
    effect_id: str,
    op: str,
    target_kind: str,
    target_id: str,
    parameters: dict[str, Any],
    units: str,
    policy_dependencies: list[str],
    start_minute: int = _DAY4_1400,
    end_minute: int | None = None,
    guard: GuardExpression | None = None,
    reversible: bool = True,
) -> TimedAction:
    return TimedAction(
        effect_id=effect_id,
        op=op,
        target_selector=TargetSelector(kind=target_kind, target_id=target_id),
        start_minute=start_minute,
        end_minute=end_minute,
        guard=guard,
        parameters=parameters,
        units=units,
        source_id="H08-TEST-PLAN",
        assumption_ids=[],
        policy_dependencies=policy_dependencies,
        reversible=reversible,
    )


def _plan(*, narrative: str = "Approved Day 4 plan", fork_minute: int = _DAY4_1400) -> DraftPlan:
    actions = [
        _timed_action(
            effect_id="temporary-toilet-outage",
            op="set_facility_availability",
            target_kind="facility",
            target_id="toilet_1",
            parameters={"working_units": 0},
            units="facility_unit",
            policy_dependencies=["facility_capacity"],
            start_minute=fork_minute,
            end_minute=fork_minute + 2,
            guard=GuardExpression(operator="gte", field="target.working_units", value=1),
        ),
        _timed_action(
            effect_id="add-temporary-toilet",
            op="add_toilet",
            target_kind="zone",
            target_id="toilet",
            parameters={"count": 1},
            units="toilet_stall",
            policy_dependencies=["facility_capacity"],
            start_minute=fork_minute,
            reversible=False,
        ),
        _timed_action(
            effect_id="add-support-staff",
            op="add_staff",
            target_kind="zone",
            target_id="information_desk",
            parameters={"count": 1},
            units="staff_member",
            policy_dependencies=["staff_operations"],
            start_minute=fork_minute,
            reversible=False,
        ),
    ]
    return DraftPlan(
        program_id="h08-approved-plan",
        narrative=narrative,
        source_text="Synthetic test preview; add one toilet and staff member.",
        simulation_start_minute=fork_minute,
        effects=actions,
        assumptions=[],
        support_mode=SupportMode.COMPOSED,
        compiler_provenance={
            "compiler_kind": "offline_rule",
            "compiler_version": "h08-test-v1",
        },
        branch_id="baseline",
        fork_at_minute=fork_minute,
        ambiguities=[],
        requires_confirmation=True,
    )


def _approved(plan: DraftPlan) -> dict[str, Any]:
    return ApprovedPlan.from_explicit_confirmation(plan).model_dump(mode="json")


def _world_hash(demo: DemoService, branch_id: str = "baseline") -> str:
    canonical = demo.branches[branch_id].runtime.world.canonical_json()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _history_before(demo: DemoService, branch_id: str, minute: int) -> list[tuple[str, int]]:
    return [
        (record.event.event_id, record.realized_minute)
        for record in demo.branches[branch_id].runtime.world.event_log.records
        if record.realized_minute < minute
    ]


@pytest.mark.long_simulation
def test_approved_plan_replays_from_checkpoint_and_is_idempotent() -> None:
    demo = _demo()
    demo.dispatch("start")
    _advance_and_pause(demo, 3000)
    _advance_and_pause(demo, _DAY4_1400 + 3)
    source_hash = _world_hash(demo)
    source_history = _history_before(demo, "baseline", _DAY4_1400)
    source_runtime = demo.branches["baseline"].runtime
    source_minute = source_runtime.world.minute_index
    source_toilets = len(source_runtime.world.facilities)
    source_staff = len(source_runtime.world.staff)

    plan = _plan()
    request = {
        "source_branch_id": "baseline",
        "approved_plan": _approved(plan),
        "idempotency_key": "review-2026-10-12-plan-01",
    }
    result = demo.dispatch("branch-plan", request)

    assert result["fork_at_minute"] == _DAY4_1400
    assert result["current_minute"] == source_minute
    assert result["source_checkpoint_id"] == "baseline-cp-0001"
    assert result["idempotent_replay"] is False
    assert len(result["receipts"][0]["event_ids"]) == 4
    child_id = result["branch_id"]
    child = demo.branches[child_id]
    assert child.runtime.parent_branch_id == "baseline"
    assert len(child.runtime.world.facilities) == source_toilets + 1
    assert len(child.runtime.world.staff) == source_staff + 1
    assert _history_before(demo, child_id, _DAY4_1400) == source_history
    assert _world_hash(demo) == source_hash

    records_by_id = {item.event.event_id: item for item in child.runtime.world.event_log.records}
    scheduled_records = [records_by_id[event_id] for event_id in result["receipts"][0]["event_ids"]]
    start_record = next(
        item
        for item in scheduled_records
        if item.event.provenance["effect_program_v1"]["phase"] == "start"
        and item.event.provenance["effect_program_v1"]["effect_id"] == "temporary-toilet-outage"
    )
    expiry_record = next(
        item
        for item in scheduled_records
        if item.event.provenance["effect_program_v1"]["phase"] == "expiry"
    )
    assert start_record.realized_minute == _DAY4_1400
    assert expiry_record.realized_minute == _DAY4_1400 + 2
    assert expiry_record.event.causation_id == start_record.event.event_id
    assert (
        child.runtime.world.facilities["toilet_1"].working_units
        == source_runtime.world.facilities["toilet_1"].working_units
    )

    scheduled_checkpoint = child.checkpoints[result["scheduled_checkpoint_id"]]
    final_checkpoint = Checkpoint.from_json(
        child.checkpoints[result["final_checkpoint_id"]].to_json()
    )
    persisted_receipt = final_checkpoint.runtime_state["approved_plan_v1"]
    assert persisted_receipt["approval_hash"] == result["plan_hash"]
    assert persisted_receipt["result"]["branch_id"] == child_id
    assert request["idempotency_key"] not in final_checkpoint.to_json()
    assert plan.source_text not in final_checkpoint.to_json()

    repeated = demo.dispatch("branch-plan", request)
    assert repeated["branch_id"] == child_id
    assert repeated["receipts"] == result["receipts"]
    assert repeated["idempotent_replay"] is True
    with pytest.raises(DemoError, match="already used for a different approved plan"):
        demo.dispatch(
            "branch-plan",
            {**request, "approved_plan": _approved(_plan(narrative="Changed preview"))},
        )

    expected_child_world = child.runtime.world.canonical_json()
    expected_child_metrics = child.runtime.metrics.to_dict()
    restored = BranchRuntime.from_checkpoint(
        Checkpoint.from_json(scheduled_checkpoint.to_json()),
        branch_id=child_id,
        parent_branch_id="baseline",
    )
    child.runtime = restored
    demo._advance_branch(child_id, child, 3)
    assert restored.world.canonical_json() == expected_child_world
    assert restored.metrics.to_dict() == expected_child_metrics


def test_invalid_approved_program_is_atomic_and_tampered_approval_is_rejected() -> None:
    demo = _demo(duration_minutes=20)
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    source_hash = _world_hash(demo)
    source_command_log = list(demo.branches["baseline"].runtime.command_log)

    invalid_plan = _plan(fork_minute=0)
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

    tampered = _approved(_plan(fork_minute=0))
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


def test_approved_plan_is_available_through_demo_http_action(tmp_path: Path) -> None:
    demo = _demo(duration_minutes=20)
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    request = {
        "source_branch_id": "baseline",
        "approved_plan": _approved(_plan(fork_minute=5)),
        "idempotency_key": "http-approved-plan-01",
    }
    raw = json.dumps(request).encode("utf-8")
    response: list[tuple[int, dict[str, Any]]] = []
    handler_type = _handler_for(InspectorService(tmp_path), demo)
    handler = handler_type.__new__(handler_type)
    handler.path = "/api/v1/demo/branch-plan"
    handler.headers = {"Content-Length": str(len(raw)), "Content-Type": "application/json"}
    handler.rfile = BytesIO(raw)
    handler._json_response = lambda status, document: response.append((status, document))

    handler.do_POST()

    assert response[0][0] == 200
    assert response[0][1]["parent_branch_id"] == "baseline"
    assert response[0][1]["branch_id"] in demo.branches
    child = demo.branches[response[0][1]["branch_id"]]
    assert child.runtime.world.minute_index == 0
    assert len(child.runtime.world.event_queue.pending()) >= 4
