"""Admin audit APIs and immutable live-branch snapshot contract.

The filtered decision and event queries over HTTP are checked in
tests/e2e/test_admin_audit_queries.py, and the admin page itself in
tests/unit/web/test_static_index.py.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from sheltersim.web.demo import DemoError, DemoService
from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _handler_for


def _run_directory(audit_inspector: InspectorService) -> Path:
    return audit_inspector.runs_directory / "run-one"


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_llm_audit_and_branch_lineage_only_return_allowlisted_metadata(
    audit_inspector: InspectorService,
) -> None:
    run_dir = _run_directory(audit_inspector)
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

    audit = audit_inspector.audit("run-one")
    lineage = audit_inspector.branch_lineage("run-one")
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
    audit_inspector: InspectorService,
) -> None:
    run_dir = _run_directory(audit_inspector)
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

    overview = audit_inspector.overview("run-one")
    events = audit_inspector.events("run-one")
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
