"""Admin audit queries over HTTP on a Run: decisions are filtered, bounded and project
nested state; events return their source and cause without the raw payload.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sheltersim.web.inspector import InspectorService, WebQueryError
from sheltersim.web.server import _dispatch_api


def _run_directory(audit_inspector: InspectorService) -> Path:
    return audit_inspector.runs_directory / "run-one"


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_decision_query_is_filtered_bounded_and_projects_nested_state(
    audit_inspector: InspectorService,
) -> None:
    _write_jsonl(
        _run_directory(audit_inspector) / "decisions.jsonl",
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
        audit_inspector,
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
        audit_inspector.decisions("run-one", limit=201)


def test_event_filters_return_source_and_cause_without_raw_payload(
    audit_inspector: InspectorService,
) -> None:
    path = _run_directory(audit_inspector) / "events_planned.json"
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
        audit_inspector,
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
        audit_inspector.events("run-one", agent_id="../private")
