"""Typed offline browser demo lifecycle, branching and API boundary tests."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.provider import LLMRouter
from sheltersim.web.demo import DemoError, DemoService
from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _handler_for

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenarios" / "failure_day3.json"


@pytest.fixture
def demo() -> DemoService:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["duration_minutes"] = 8
    document["scheduled_events"] = [
        {**event, "due_minute": 2} for event in document["scheduled_events"]
    ]
    return DemoService(ScenarioSpec.model_validate(document))


def test_offline_demo_pauses_forks_intervenes_rewinds_and_compares(demo: DemoService) -> None:
    started = demo.dispatch("start")
    assert started["offline"] is True
    assert started["branches"][0]["branch_id"] == "baseline"
    assert started["branches"][0]["status"] == "running"

    demo.dispatch("advance", {"branch_id": "baseline", "steps": 2})
    paused = demo.dispatch("pause", {"branch_id": "baseline"})
    baseline = paused["branches"][0]
    assert baseline["minute"] == 2
    assert baseline["latest_checkpoint_id"]
    pending_event_ids = {
        event.event_id for event in demo.branches["baseline"].runtime.world.event_queue.pending()
    }
    assert {"day3-toilet3-outage", "day3-toilet4-outage"} <= pending_event_ids

    demo.dispatch("checkpoint", {"branch_id": "baseline"})
    forked = demo.dispatch("fork", {"branch_id": "baseline", "new_branch_id": "staff-plus"})
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
    changed = demo.dispatch("command", {"branch_id": "staff-plus", "command": command})
    changed_child = next(item for item in changed["branches"] if item["branch_id"] == "staff-plus")
    hash_before_rejection = changed_child["state_hash"]

    # A batch is applied to an isolated candidate and cannot partially commit.
    batch_before = hash_before_rejection
    with pytest.raises(DemoError, match="unknown staff zone"):
        demo.dispatch(
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
    child_runtime = demo.branches["staff-plus"].runtime
    assert "batch-temp" not in child_runtime.world.staff
    assert child_runtime.world.canonical_json()
    assert demo.state()["operations"][-1]["state_hash_before"] == batch_before
    assert demo.state()["operations"][-1]["state_hash_after"] == batch_before

    with pytest.raises(DemoError, match="already exists"):
        demo.dispatch("command", {"branch_id": "staff-plus", "command": command})
    rejected = demo.state()["operations"][-1]
    assert rejected["accepted"] is False
    assert rejected["state_hash_before"] == rejected["state_hash_after"] == hash_before_rejection

    # The intervention checkpoint can be rewound to the original fork image.
    demo.dispatch("checkpoint", {"branch_id": "staff-plus"})
    demo.dispatch("rewind", {"branch_id": "staff-plus", "checkpoint_id": "staff-plus-cp-0000"})
    rewound = next(item for item in demo.state()["branches"] if item["branch_id"] == "staff-plus")
    assert rewound["staff_count"] == baseline["staff_count"]
    demo.dispatch("command", {"branch_id": "staff-plus", "command": command})

    for branch_id in ("baseline", "staff-plus"):
        demo.dispatch("resume", {"branch_id": branch_id})
        completed = demo.dispatch("advance", {"branch_id": branch_id, "steps": 8})
        branch = next(item for item in completed["branches"] if item["branch_id"] == branch_id)
        assert branch["status"] == "completed"
        assert branch["minute"] == 8

    comparison = demo.dispatch(
        "compare", {"baseline_branch_id": "baseline", "candidate_branch_id": "staff-plus"}
    )
    assert comparison["baseline_branch_id"] == "baseline"
    assert comparison["candidate_branch_id"] == "staff-plus"
    assert comparison["no_op_equivalent"] is False
    assert "first_divergence_minute" in comparison
    assert "source_ids_json" not in json.dumps(comparison)


def test_demo_command_schema_and_state_transitions_are_strict(demo: DemoService) -> None:
    with pytest.raises(DemoError, match="start the demo"):
        demo.dispatch("pause", {"branch_id": "baseline"})
    demo.dispatch("start")
    with pytest.raises(DemoError, match="unsupported"):
        demo.dispatch("advance", {"branch_id": "baseline", "steps": 1, "unexpected": True})
    with pytest.raises(DemoError, match="integer"):
        demo.dispatch("advance", {"branch_id": "baseline", "steps": True})
    with pytest.raises(DemoError, match="must be paused"):
        demo.dispatch("command", {"branch_id": "baseline", "command": {}})


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


def test_explicit_story_review_route_is_offline_and_does_not_mutate_branches(
    demo: DemoService, tmp_path: Path
) -> None:
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    demo.dispatch("fork", {"branch_id": "baseline", "new_branch_id": "candidate"})
    for branch_id in ("baseline", "candidate"):
        demo.dispatch("resume", {"branch_id": branch_id})
        demo.dispatch("advance", {"branch_id": branch_id, "steps": 8})
    demo._story_router = LLMRouter(LLMSettings(mode=LLMMode.OFFLINE))
    before_review = demo.state()

    handler_type = _handler_for(InspectorService(tmp_path), demo)
    handler = handler_type.__new__(handler_type)
    request = json.dumps(
        {"baseline_branch_id": "baseline", "candidate_branch_id": "candidate"}
    ).encode()
    responses: list[tuple[int, dict]] = []
    handler.path = "/api/v1/demo/story-review"
    handler.headers = {"Content-Length": str(len(request)), "Content-Type": "application/json"}
    handler.rfile = BytesIO(request)
    handler._json_response = lambda status, document: responses.append((status, document))
    handler.do_POST()

    assert responses[0][0] == 200
    assert responses[0][1]["card"]["mode"] == "offline"
    assert responses[0][1]["review_status"] == "rule_fallback"
    assert responses[0][1]["usage"]["api_request_count"] == 0
    assert demo.state() == before_review


def test_packaged_demo_scenario_is_the_offline_day3_fixture() -> None:
    from sheltersim.web.demo import load_demo_scenario

    scenario = load_demo_scenario()
    assert scenario.id == "failure_day3"
    assert len(scenario.profiles) == 100
    assert {event.due_minute for event in scenario.scheduled_events} == {2880}
