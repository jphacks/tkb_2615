"""Approved EffectPrograms fork from checkpoints without mutating their source, replay
idempotently, and are available through the demo HTTP action.

An invalid or tampered approval is checked in tests/unit/experiments/test_branch.py.
"""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.llm.schemas import ApprovedPlan, DraftPlan
from sheltersim.web.demo import DemoError, DemoService
from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _handler_for

_DAY4_1400 = 3 * 1440 + 14 * 60


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
def test_approved_plan_replays_from_checkpoint_and_is_idempotent(
    small_replay_demo, day4_plan
) -> None:
    demo = small_replay_demo()
    demo.dispatch("start")
    _advance_and_pause(demo, 3000)
    _advance_and_pause(demo, _DAY4_1400 + 3)
    source_hash = _world_hash(demo)
    source_history = _history_before(demo, "baseline", _DAY4_1400)
    source_runtime = demo.branches["baseline"].runtime
    source_minute = source_runtime.world.minute_index
    source_toilets = len(source_runtime.world.facilities)
    source_staff = len(source_runtime.world.staff)

    plan = day4_plan()
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
            {**request, "approved_plan": _approved(day4_plan(narrative="Changed preview"))},
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


def test_approved_plan_is_available_through_demo_http_action(
    tmp_path: Path, small_replay_demo, day4_plan
) -> None:
    demo = small_replay_demo(duration_minutes=20)
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    request = {
        "source_branch_id": "baseline",
        "approved_plan": _approved(day4_plan(fork_minute=5)),
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
