"""Admin audit APIs and immutable live-branch snapshot contract."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.web.demo import DemoError, DemoService
from sheltersim.web.inspector import InspectorService, WebQueryError
from sheltersim.web.server import _dispatch_api, _handler_for

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


@pytest.fixture
def inspector(tmp_path: Path) -> InspectorService:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["id"] = "admin-audit-test"
    document["duration_minutes"] = 2
    document["profiles"] = document["profiles"][:4]
    document["zones"] = [
        {key: value for key, value in zone.items() if key not in {"display_x", "display_y"}}
        for zone in document["zones"]
    ]
    document["scheduled_events"] = []
    scenario = ScenarioSpec.model_validate(document)
    run_scenario(scenario, tmp_path / "run-one")
    return InspectorService(tmp_path)


def _run_directory(inspector: InspectorService) -> Path:
    return inspector.runs_directory / "run-one"


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_decision_query_is_filtered_bounded_and_projects_nested_state(
    inspector: InspectorService,
) -> None:
    _write_jsonl(
        _run_directory(inspector) / "decisions.jsonl",
        [
            {
                "minute_index": 0,
                "agent_id": "A001",
                "action": "seek_help",
                "trigger": "need threshold reached",
                "target_id": "support-desk",
                "api_key": "secret-value",
                "prompt": "private prompt",
                "observed_state": {
                    "zone_id": "sleeping",
                    "activity": "resting",
                    "needs": {"hunger": 0.4, "private_note": "name@example.invalid"},
                    "stress": 0.2,
                    "private_note": "name@example.invalid",
                },
                "candidates": [
                    {
                        "action": "seek_help",
                        "eligible": True,
                        "probability": 1.0,
                        "authorization": "secret-value",
                    }
                ],
            },
            {"minute_index": 1, "agent_id": "A002", "action": "move"},
        ],
    )

    result = _dispatch_api(
        inspector,
        "/api/v1/runs/run-one/decisions",
        {"agent_id": ["A001"], "at_minute": ["0"], "limit": ["1"]},
    )
    encoded = json.dumps(result)

    assert result["total"] == 1
    assert result["items"][0]["agent_id"] == "A001"
    assert result["items"][0]["observed_state"]["needs"] == {"hunger": 0.4}
    assert "private_note" not in encoded
    assert "name@example.invalid" not in encoded
    assert "secret-value" not in encoded
    assert "private prompt" not in encoded
    with pytest.raises(WebQueryError, match="limit"):
        inspector.decisions("run-one", limit=201)


def test_event_filters_return_source_and_cause_without_raw_payload(
    inspector: InspectorService,
) -> None:
    path = _run_directory(inspector) / "events_planned.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "events": [
                    {
                        "id": "planned-a",
                        "due_minute": 1,
                        "event_type": "water_outage",
                        "source_id": "staff-report",
                        "causation_id": "event-parent",
                        "payload": {
                            "agent_id": "A001",
                            "zone_id": "sleeping",
                            "raw_text": "private prompt",
                        },
                        "provenance": {"branch_id": "branch-a"},
                    },
                    {
                        "id": "planned-b",
                        "due_minute": 1,
                        "event_type": "water_outage",
                        "payload": {"agent_id": "A002", "zone_id": "sleeping"},
                        "provenance": {"branch_id": "branch-a"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = _dispatch_api(
        inspector,
        "/api/v1/runs/run-one/events",
        {
            "status": ["planned"],
            "agent_id": ["A001"],
            "zone_id": ["sleeping"],
            "branch_id": ["branch-a"],
            "limit": ["10"],
        },
    )
    assert [item["event_id"] for item in result["items"]] == ["planned-a"]
    event = result["items"][0]
    assert event["source_id"] == "staff-report"
    assert event["causation_id"] == "event-parent"
    assert "raw_text" not in json.dumps(event)
    with pytest.raises(WebQueryError, match="agent_id"):
        inspector.events("run-one", agent_id="../private")


def test_llm_audit_and_branch_lineage_only_return_allowlisted_metadata(
    inspector: InspectorService,
) -> None:
    run_dir = _run_directory(inspector)
    _write_jsonl(
        run_dir / "llm_calls.jsonl",
        [
            {
                "call_index": 1,
                "request_id": "req-test-1",
                "stage": "PREPARE",
                "outcome": "fallback",
                "model_id": "claude-haiku-test",
                "input_hash": "a" * 64,
                "state_hash": "b" * 64,
                "prompt_version": "prepare-v1",
                "schema_version": 1,
                "api_request_count": 1,
                "input_tokens": 12,
                "output_tokens": 3,
                "estimated_cost_usd": 0.01,
                "elapsed_ms": 25.0,
                "fallback_used": True,
                "input_text": "private prompt",
                "api_key": "secret-value",
            }
        ],
    )
    _write_jsonl(
        run_dir / "social_decisions.jsonl",
        [
            {
                "minute": 30,
                "agent_id": "A001",
                "trigger": "long_waiting_support_request",
                "interaction_type": "complaint_response",
                "target_agent_id": None,
                "trigger_id": "support-request-1",
                "action": "explain_staff_process",
                "accepted": True,
                "status": "accepted",
                "reason": "accepted_and_scheduled",
                "reason_short": "long_wait",
                "effect_code": "requester_stress_relief_and_trust_increase",
                "event_id": "social-interaction-1",
                "model_id": "claude-haiku-test",
                "input_hash": "d" * 64,
                "api_request_count": 1,
                "input_tokens": 50,
                "output_tokens": 8,
                "estimated_cost_usd": 0.000009,
                "source_id": "ASM-EVENT-TRIGGERED-SOCIAL-INTERACTIONS-V1",
                "cache_hit": False,
                "fallback_used": False,
                "prompt": "private prompt",
            }
        ],
    )
    (run_dir / "branch_lineage.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "branch_id": "branch-a",
                "parent_branch_id": "main",
                "first_divergence_minute": 1,
                "state_hash": "c" * 64,
                "source_text": "private prompt",
                "extra": {"api_key": "secret-value"},
            }
        ),
        encoding="utf-8",
    )

    audit = inspector.audit("run-one")
    lineage = inspector.branch_lineage("run-one")
    encoded = json.dumps({"audit": audit, "lineage": lineage})

    assert audit["llm_calls"][0]["request_id"] == "req-test-1"
    assert audit["llm_calls"][0]["prompt_version"] == "prepare-v1"
    assert audit["llm_calls"][0]["schema_version"] == 1
    assert audit["social_decisions"][0]["interaction_type"] == "complaint_response"
    assert audit["social_decisions"][0]["effect_code"] == (
        "requester_stress_relief_and_trust_increase"
    )
    assert audit["social_decisions"][0]["estimated_cost_usd"] == 0.000009
    assert audit["social_decisions"][0]["input_hash"] == "d" * 64
    assert lineage["recorded"] is True
    assert lineage["lineage"]["first_divergence_minute"] == 1
    assert "private prompt" not in encoded
    assert "secret-value" not in encoded


def test_overview_redacts_nested_private_fields_and_event_status_counts(
    inspector: InspectorService,
) -> None:
    run_dir = _run_directory(inspector)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["model_configs"] = {
        "mode": "offline",
        "api_key": "secret-value",
        "api_token": "another-secret",
        "nested": {
            "contact_email": "name@example.invalid",
            "person_name": "Private Name",
            "trace_level": "summary",
        },
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (run_dir / "events_planned.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "events": [
                    {
                        "id": "planned-a",
                        "due_minute": 1,
                        "event_type": "water_outage",
                        "payload": {"zone_id": "sleeping"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        run_dir / "events_realized.jsonl",
        [
            {
                "realized_minute": minute,
                "status": status,
                "event": {
                    "event_id": "event-" + status,
                    "event_type": "water_outage",
                    "due_minute": minute,
                    "payload": {"zone_id": "sleeping"},
                },
            }
            for minute, status in enumerate(("applied", "suppressed", "failed", "rejected"), 2)
        ],
    )

    overview = inspector.overview("run-one")
    events = inspector.events("run-one")
    encoded = json.dumps(overview)

    assert overview["manifest"]["model_configs"]["mode"] == "offline"
    assert overview["manifest"]["model_configs"]["nested"]["trace_level"] == "summary"
    assert "secret-value" not in encoded
    assert "another-secret" not in encoded
    assert "name@example.invalid" not in encoded
    assert "Private Name" not in encoded
    assert events["status_counts"] == {
        "planned": 1,
        "applied": 1,
        "suppressed": 1,
        "failed": 1,
        "rejected": 1,
    }


def test_demo_snapshot_is_frozen_detached_and_get_only(tmp_path: Path) -> None:
    demo = DemoService()
    demo.dispatch("start")
    before = demo.state()
    agent_id = demo.scenario.profiles[0].id
    view = demo.snapshot_view("baseline", at_minute=12, agent_id=agent_id)
    payload = view.to_dict()
    payload["branch"]["status"] = "completed"
    payload["world"]["state_hash"] = "changed"

    reread = demo.snapshot_view("baseline", at_minute=12, agent_id=agent_id).to_dict()
    assert reread["source"] == "immutable_checkpoint_snapshot"
    assert reread["captured_minute"] == 0
    assert reread["minute_match"] is False
    assert reread["branch"]["status"] == "running"
    assert reread["selected_agent"]["id"] == agent_id
    assert reread["selected_agent"]["source"].startswith("checkpoint WorldState snapshot")
    assert demo.state() == before
    with pytest.raises(FrozenInstanceError):
        view._document_json = "{}"  # type: ignore[misc]
    with pytest.raises(DemoError, match="unknown branch_id"):
        demo.snapshot_view("missing")

    handler_type = _handler_for(InspectorService(tmp_path), demo)
    handler = handler_type.__new__(handler_type)
    response: list[tuple[int, dict[str, Any]]] = []
    handler._json_response = lambda status, document: response.append((status, document))
    handler.path = "/api/v1/demo/snapshot?branch_id=baseline&minute=12&agent_id=" + agent_id
    handler.do_GET()
    assert response[0][0] == 200
    assert response[0][1]["world"]["state_hash"] == reread["world"]["state_hash"]

    handler.path = (
        "/api/v1/demo/comparison?baseline_branch_id=baseline&candidate_branch_id=baseline"
    )
    handler.do_GET()
    assert response[-1][0] == 409
    assert demo.state() == before


def test_admin_shell_has_keyboard_tabs_and_interactive_controls_are_demo_only() -> None:
    root = Path(__file__).parents[1]
    html = (root / "src/sheltersim/web/static/index.html").read_text(encoding="utf-8")
    css = (root / "src/sheltersim/web/static/style.css").read_text(encoding="utf-8")

    assert 'role="tablist"' in html
    assert 'data-admin-tab="overview"' in html
    assert 'data-admin-panel="decisions"' in html
    assert 'id="admin-panel-world" role="tabpanel" aria-labelledby="tab-world" hidden' in html
    assert 'id="admin-branch-snapshot"' in html
    assert 'class="card demo-card demo-only"' in html
    assert 'body[data-mode="admin"] .demo-only { display: none !important; }' in css
