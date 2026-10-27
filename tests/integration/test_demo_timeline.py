"""Saved-artifact contracts for the synchronized demo timeline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.web.inspector import InspectorService, WebQueryError
from sheltersim.web.server import _dispatch_api


@pytest.fixture
def inspector(tmp_path: Path) -> InspectorService:
    run = tmp_path / "demo-run"
    run.mkdir()
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "scenario_id": "timeline-test",
                "seed": 2026,
                "duration_minutes": 10,
                "final_minute": 4,
                "run_status": "completed",
                "execution_mode": "offline",
                "branch_id": "main",
            }
        ),
        encoding="utf-8",
    )
    (run / "agents_initial.json").write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "id": "A_001",
                        "group_id": "G1",
                        "initial_zone_id": "sleeping",
                        "mobility": "independent",
                        "initial_needs": {"toilet_urgency": 0.2, "hunger": 0.1, "fatigue": 0.3},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (run / "zones.json").write_text(
        json.dumps(
            {
                "zones": [
                    {
                        "id": "sleeping",
                        "name": "居住区",
                        "capacity": 100,
                        "display_x": 0.2,
                        "display_y": 0.2,
                    }
                ],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    (run / "metrics.csv").write_text(
        "minute_index,toilet_queue_length,stress_mean,stress_high_count,staff_busy_count,staff_load_ratio,staff_assigned_task_count,private_column\n"
        "0,1,0.25,0,1,0.5,1,should-not-leak\n"
        "2,3,0.75,2,2,1.0,4,should-not-leak\n",
        encoding="utf-8",
    )
    (run / "operational_kpis.csv").write_text(
        "minute_index,water_queue_length,meal_queue_length,water_ml_remaining,food_meal_units_remaining,private_column\n"
        "0,2,1,9000,12,should-not-leak\n"
        "2,4,3,7000,9,should-not-leak\n",
        encoding="utf-8",
    )
    (run / "events_planned.json").write_text(
        json.dumps(
            {
                "events": [
                    {
                        "id": "planned-water",
                        "due_minute": 4,
                        "priority": 0,
                        "sequence": 1,
                        "event_type": "water_delivery",
                        "payload": {
                            "agent_id": "A_001",
                            "zone_id": "sleeping",
                            "facility_id": "water_station_main",
                            "narrative": "should-not-leak",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (run / "events_realized.jsonl").write_text(
        json.dumps(
            {
                "event": {
                    "event_id": "realized-water",
                    "due_minute": 1,
                    "event_type": "water_outage",
                    "payload": {
                        "agent_id": "A_001",
                        "zone_id": "sleeping",
                        "facility_id": "water_station_main",
                        "source_ids": ["should-not-leak"],
                    },
                    "provenance": {"branch_id": "main"},
                },
                "realized_minute": 3,
                "created_minute": 2,
                "status": "applied",
                "reason": "test reason",
            }
        ),
        encoding="utf-8",
    )
    (run / "decisions.jsonl").write_text(
        json.dumps(
            {
                "agent_id": "A_001",
                "minute_index": 2,
                "action": "seek_help",
                "selected_action": "seek_help",
                "trigger": "support_request",
                "observed_state": {
                    "zone_id": "sleeping",
                    "activity": "seeking_help",
                    "stress": 0.8,
                    "needs": {"toilet_urgency": 0.4, "hunger": 0.2, "fatigue": 0.1},
                    "queue": {"kind": "water", "wait_minutes": 12},
                    "support": {"status": "pending", "wait_minutes": 7, "reason": "private"},
                },
            }
        ),
        encoding="utf-8",
    )
    return InspectorService(tmp_path)


def test_timeline_selects_latest_recorded_minute_and_merges_allowlisted_kpis(
    inspector: InspectorService,
) -> None:
    timeline = inspector.timeline("demo-run", at_minute=3, limit=1)

    assert timeline["selected"] == {
        "minute_index": 2,
        "toilet_queue_length": 3.0,
        "stress_mean": 0.75,
        "stress_high_count": 2.0,
        "staff_busy_count": 2.0,
        "staff_load_ratio": 1.0,
        "staff_assigned_task_count": 4.0,
        "water_queue_length": 4.0,
        "meal_queue_length": 3.0,
        "water_ml_remaining": 7000.0,
        "food_meal_units_remaining": 9.0,
    }
    assert "private_column" not in json.dumps(timeline)

    routed = _dispatch_api(
        inspector,
        "/api/v1/runs/demo-run/timeline",
        {"at_minute": ["3"], "limit": ["1"]},
    )
    assert routed["selected"]["minute_index"] == 2
    with pytest.raises(WebQueryError, match="at_minute"):
        inspector.timeline("demo-run", at_minute=-1)


def test_event_times_relations_and_agent_support_are_safe_projections(
    inspector: InspectorService,
) -> None:
    events = inspector.events("demo-run")["items"]
    planned = next(item for item in events if item["event_id"] == "planned-water")
    realized = next(item for item in events if item["event_id"] == "realized-water")

    assert (planned["created_minute"], planned["scheduled_minute"], planned["realized_minute"]) == (
        None,
        4,
        None,
    )
    assert (
        realized["created_minute"],
        realized["scheduled_minute"],
        realized["realized_minute"],
    ) == (
        2,
        1,
        3,
    )
    assert planned["related_agent_id"] == "A_001"
    assert planned["related_zone_id"] == "sleeping"
    assert planned["related_facility_id"] == "water_station_main"
    assert "should-not-leak" not in json.dumps(events)
    assert "payload" not in planned

    initial = inspector.agent("demo-run", "A_001", at_minute=0)
    assert initial["state"]["stress"] is None

    initial_map = inspector.zone_map("demo-run", at_minute=0)
    initial_point = next(item for item in initial_map["agents"] if item["id"] == "A_001")
    assert initial_point["stress"] is None
    assert initial_point["queue_wait_minutes"] is None

    agent = inspector.agent("demo-run", "A_001", at_minute=3)
    assert agent["state"]["queue_kind"] == "water"
    assert agent["state"]["queue_wait_minutes"] == 12
    assert agent["state"]["support_status"] == "pending"
    assert agent["state"]["support_wait_minutes"] == 7

    map_data = inspector.zone_map("demo-run", at_minute=3)
    point = next(item for item in map_data["agents"] if item["id"] == "A_001")
    assert point["support_status"] == "pending"
    assert point["support_wait_minutes"] == 7
