"""The saved-run inspector over hand-written or finished Runs, and a socketless request handler."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from support.paths import SCENARIO_100_AGENTS


@pytest.fixture
def demo_timeline_inspector(tmp_path: Path) -> Any:
    """An Inspector over one hand-written demo Run (demo-run): manifest, profiles, metrics,
    events and decisions for a four-minute timeline with private fields to redact.
    """
    from sheltersim.web.inspector import InspectorService

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


@pytest.fixture
def audit_inspector(tmp_path: Path) -> Any:
    """An Inspector over one finished Run (run-one): four people of scenario_100_agents
    for two minutes, without display coordinates or scheduled events.
    """
    from sheltersim.experiments.runner import run_scenario
    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.web.inspector import InspectorService

    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
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


@pytest.fixture
def full_trace_inspector(tmp_path: Path) -> Any:
    """An Inspector over one finished Run (run-one) of all 100 people of scenario_100_agents
    for two minutes at the full trace level, with a planned event whose raw detail must
    not be returned.
    """
    from sheltersim.core.engine import TraceLevel
    from sheltersim.experiments.runner import run_scenario
    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.web.inspector import InspectorService

    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["duration_minutes"] = 2
    document["profiles"] = document["profiles"][:100]
    document["zones"] = [
        {key: value for key, value in zone.items() if key not in {"display_x", "display_y"}}
        for zone in document["zones"]
    ]
    document["scheduled_events"] = [
        {
            "id": "planned-test",
            "kind": "exogenous",
            "due_minute": 0,
            "priority": 0,
            "sequence": 0,
            "event_type": "unknown_test_event",
            "payload": {"raw_detail": "must-not-be-returned"},
        }
    ]
    scenario = ScenarioSpec.model_validate(document)
    run_scenario(scenario, tmp_path / "run-one", trace_level=TraceLevel.FULL)
    return InspectorService(tmp_path)


def _bare_request_handler(tmp_path: Path) -> tuple[Any, list[int], dict[str, str], Any]:
    from io import BytesIO

    from sheltersim.web.demo import DemoService
    from sheltersim.web.inspector import InspectorService
    from sheltersim.web.server import _handler_for

    handler_type = _handler_for(InspectorService(tmp_path), DemoService())
    handler = handler_type.__new__(handler_type)
    statuses: list[int] = []
    headers: dict[str, str] = {}
    output = BytesIO()
    handler.send_response = statuses.append
    handler.send_header = lambda name, value: headers.__setitem__(name, value)
    handler.end_headers = lambda: None
    handler.wfile = output
    return handler, statuses, headers, output


@pytest.fixture
def bare_request_handler() -> Callable[[Path], tuple[Any, list[int], dict[str, str], Any]]:
    """Make a web request handler without a socket; return it with the statuses, headers and
    body it writes.
    """
    return _bare_request_handler
