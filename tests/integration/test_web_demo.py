"""The offline browser demo pauses, forks, intervenes, rewinds and compares, including
movement and capacity-wait KPIs, and its HTTP mutations stay separate from the read-only
saved-run API.

The command schema and the packaged scenario are checked in tests/unit/web/test_demo.py,
and the story review route in tests/e2e/test_web_demo_story_review.py.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest

from sheltersim.web.demo import DemoError, DemoService
from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _handler_for


def test_offline_demo_pauses_forks_intervenes_rewinds_and_compares(day3_demo: DemoService) -> None:
    started = day3_demo.dispatch("start")
    assert started["offline"] is True
    assert started["branches"][0]["branch_id"] == "baseline"
    assert started["branches"][0]["status"] == "running"

    day3_demo.dispatch("advance", {"branch_id": "baseline", "steps": 2})
    paused = day3_demo.dispatch("pause", {"branch_id": "baseline"})
    baseline = paused["branches"][0]
    assert baseline["minute"] == 2
    assert baseline["latest_checkpoint_id"]
    pending_event_ids = {
        event.event_id
        for event in day3_demo.branches["baseline"].runtime.world.event_queue.pending()
    }
    assert {"day3-toilet3-outage", "day3-toilet4-outage"} <= pending_event_ids

    day3_demo.dispatch("checkpoint", {"branch_id": "baseline"})
    forked = day3_demo.dispatch("fork", {"branch_id": "baseline", "new_branch_id": "staff-plus"})
    child = next(item for item in forked["branches"] if item["branch_id"] == "staff-plus")
    assert child["parent_branch_id"] == "baseline"
    assert child["minute"] == 2

    staff_id = f"demo-staff-{child['staff_count'] + 1:02d}"
    command = {
        "kind": "add_staff",
        "staff_id": staff_id,
        "zone_id": "sleeping",
        "skills": ["support"],
    }
    changed = day3_demo.dispatch("command", {"branch_id": "staff-plus", "command": command})
    changed_child = next(item for item in changed["branches"] if item["branch_id"] == "staff-plus")
    hash_before_rejection = changed_child["state_hash"]

    # A batch is applied to an isolated candidate and cannot partially commit.
    batch_before = hash_before_rejection
    with pytest.raises(DemoError, match="unknown staff zone"):
        day3_demo.dispatch(
            "commands",
            {
                "branch_id": "staff-plus",
                "commands": [
                    {
                        "kind": "add_staff",
                        "staff_id": "batch-temp",
                        "zone_id": "sleeping",
                        "skills": ["support"],
                    },
                    {
                        "kind": "add_staff",
                        "staff_id": "batch-invalid",
                        "zone_id": "missing-zone",
                        "skills": ["support"],
                    },
                ],
            },
        )
    child_runtime = day3_demo.branches["staff-plus"].runtime
    assert "batch-temp" not in child_runtime.world.staff
    assert child_runtime.world.canonical_json()
    assert day3_demo.state()["operations"][-1]["state_hash_before"] == batch_before
    assert day3_demo.state()["operations"][-1]["state_hash_after"] == batch_before

    with pytest.raises(DemoError, match="already exists"):
        day3_demo.dispatch("command", {"branch_id": "staff-plus", "command": command})
    rejected = day3_demo.state()["operations"][-1]
    assert rejected["accepted"] is False
    assert rejected["state_hash_before"] == rejected["state_hash_after"] == hash_before_rejection

    # The intervention checkpoint can be rewound to the original fork image.
    day3_demo.dispatch("checkpoint", {"branch_id": "staff-plus"})
    day3_demo.dispatch("rewind", {"branch_id": "staff-plus", "checkpoint_id": "staff-plus-cp-0000"})
    rewound = next(
        item for item in day3_demo.state()["branches"] if item["branch_id"] == "staff-plus"
    )
    assert rewound["staff_count"] == baseline["staff_count"]
    day3_demo.dispatch("command", {"branch_id": "staff-plus", "command": command})

    for branch_id in ("baseline", "staff-plus"):
        day3_demo.dispatch("resume", {"branch_id": branch_id})
        completed = day3_demo.dispatch("advance", {"branch_id": branch_id, "steps": 8})
        branch = next(item for item in completed["branches"] if item["branch_id"] == branch_id)
        assert branch["status"] == "completed"
        assert branch["minute"] == 8

    comparison = day3_demo.dispatch(
        "compare", {"baseline_branch_id": "baseline", "candidate_branch_id": "staff-plus"}
    )
    assert comparison["baseline_branch_id"] == "baseline"
    assert comparison["candidate_branch_id"] == "staff-plus"
    assert comparison["no_op_equivalent"] is False
    assert "first_divergence_minute" in comparison
    assert "source_ids_json" not in json.dumps(comparison)


def test_demo_comparison_reports_movement_and_capacity_wait_kpis_with_event_evidence(
    small_replay_scenario, route_edit_command
) -> None:
    # The demo hosts only its packaged gym layout, so a generated Scenario cannot be
    # used here (#137). Until #137 this was a unit test that wrote the travel and
    # capacity-wait records by hand instead of running both Branches.
    demo = DemoService(small_replay_scenario(600))
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    # One hour, one person at a time: long enough for people to queue for the corridor.
    command = route_edit_command(travel_minutes=60)
    candidate_id = demo.dispatch(
        "layout-branch",
        {
            "source_branch_id": "baseline",
            "idempotency_key": "route-kpi-test",
            "commands": [command],
        },
    )["branch_id"]
    for branch_id in ("baseline", candidate_id):
        demo.dispatch("resume", {"branch_id": branch_id})
        while demo.branches[branch_id].status == "running":
            demo.dispatch("advance", {"branch_id": branch_id, "steps": 120})

    report = demo.dispatch(
        "compare", {"baseline_branch_id": "baseline", "candidate_branch_id": candidate_id}
    )

    metrics = report["routing_metrics"]
    assert metrics["travel_minutes_total"]["delta"] > 0
    assert metrics["capacity_wait_observation_count"]["baseline"] == 0
    assert metrics["capacity_wait_observation_count"]["delta"] > 0
    evidence = report["routing_evidence"]["candidate"]
    assert any(
        command["edge_id"] in item["edge_ids"] and item["travel_minutes"] >= 60
        for item in evidence["completed_travel_events"]
    )
    assert evidence["capacity_wait_events"]
    for item in evidence["capacity_wait_events"]:
        assert item["edge_id"] == command["edge_id"]
        assert item["source_id"] == "ASM-A05-ROUTE-EDIT-V1"


def test_demo_http_mutations_are_separate_from_read_only_saved_run_api(tmp_path: Path) -> None:
    handler_type = _handler_for(InspectorService(tmp_path), DemoService())
    handler = handler_type.__new__(handler_type)
    response = []
    handler.path = "/api/v1/demo/start"
    handler.headers = {"Content-Length": "2", "Content-Type": "application/json"}
    handler.rfile = BytesIO(b"{}")
    handler._json_response = lambda status, document: response.append((status, document))
    handler.do_POST()
    assert response[0][0] == 200
    assert response[0][1]["offline"] is True

    response.clear()
    handler.path = "/api/v1/runs/run-1"
    handler.do_POST()
    assert response == [(405, {"error": "inspector API is read-only"})]
