"""Saved-run web inspector queries, privacy allowlists, and HTTP read-only gate."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest

from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.web.inspector import InspectorService, WebQueryError
from sheltersim.web.server import _dispatch_api, _handler_for

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


@pytest.fixture
def inspector(tmp_path: Path) -> InspectorService:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
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


def test_saved_run_supports_profiles_agent_events_kpis_and_synthetic_map(
    inspector: InspectorService,
) -> None:
    run_id = inspector.list_runs()["runs"][0]["id"]
    overview = inspector.overview(run_id)
    profiles = inspector.profiles(run_id, limit=100)
    agent_id = profiles["items"][0]["id"]
    agent = inspector.agent(run_id, agent_id, at_minute=2)
    timeline = inspector.timeline(run_id, limit=20)
    planned = inspector.events(run_id, status="planned")
    realized = inspector.events(run_id, status="realized")
    map_document = inspector.zone_map(run_id, at_minute=2, selected_agent_id=agent_id)

    assert overview["profile_count"] == 100
    assert len(profiles["items"]) == 100
    assert agent["profile"]["id"] == agent_id
    assert agent["state"]["source"] == "pre-decision snapshot"
    assert agent["decisions"]
    assert timeline["items"][-1]["minute_index"] == 1
    assert all("source_ids_json" not in row for row in timeline["items"])
    assert {
        "support_wait_staff_busy_request_minutes",
        "support_wait_staff_off_shift_request_minutes",
    } <= set(timeline["items"][-1])
    assert planned["items"][0]["event_id"] == "planned-test"
    assert "payload" not in planned["items"][0]
    assert realized["items"]
    assert map_document["synthetic_layout"] is True
    assert map_document["selected_agent"]["id"] == agent_id
    assert all(zone["occupancy"] >= 0 for zone in map_document["zones"])
    audit = inspector.audit(run_id)
    assert audit["llm_calls"] == []
    assert "input_text" not in json.dumps(audit)


def test_agent_story_cards_link_only_recorded_states_decisions_and_events(
    inspector: InspectorService,
) -> None:
    run_id = inspector.list_runs()["runs"][0]["id"]

    stories = inspector.agent_stories(run_id, at_minute=2)
    api_stories = _dispatch_api(
        inspector,
        f"/api/v1/runs/{run_id}/agent-stories",
        {"at_minute": ["2"]},
    )

    assert stories == api_stories
    assert stories["decision_trace_available"] is True
    assert [item["criterion_id"] for item in stories["stories"]] == [
        "support_need",
        "mobility_support",
        "family_context",
    ]
    assert all(item["selection_reason"] for item in stories["stories"])
    assert all(len(item["history"]) <= 8 for item in stories["stories"])
    assert all(item["dialogue"] is None for item in stories["stories"])
    assert all(
        inspector.event(run_id, event["event_id"])["records"]
        for story in stories["stories"]
        for event in story["event_references"]
    )
    with pytest.raises(WebQueryError, match="duration"):
        inspector.agent_stories(run_id, at_minute=3)


def test_query_filters_are_bounded_and_run_ids_cannot_escape_root(
    inspector: InspectorService,
) -> None:
    run_id = inspector.list_runs()["runs"][0]["id"]
    assert inspector.profiles(run_id, query="A001", limit=100)["total"] == 1
    with pytest.raises(WebQueryError, match="limit"):
        inspector.profiles(run_id, limit=101)
    with pytest.raises(WebQueryError, match="characters"):
        inspector.overview("../outside")
    with pytest.raises(WebQueryError, match="does not exist"):
        inspector.agent(run_id, "unknown-agent")
    with pytest.raises(WebQueryError, match="duration"):
        inspector.zone_map(run_id, at_minute=99)


def test_api_dispatch_is_allowlisted_and_reports_unknown_routes(
    inspector: InspectorService,
) -> None:
    run_id = inspector.list_runs()["runs"][0]["id"]
    result = _dispatch_api(
        inspector,
        f"/api/v1/runs/{run_id}/profiles",
        {"limit": ["100"]},
    )
    encoded = json.dumps(result)
    assert "must-not-be-returned" not in encoded
    assert len(result["items"]) == 100
    with pytest.raises(WebQueryError, match="unknown API"):
        _dispatch_api(inspector, "/api/v1/runs/nope/mutate", {})


def test_http_handler_serves_ui_and_rejects_mutating_requests(
    inspector: InspectorService,
) -> None:
    handler_type = _handler_for(inspector)
    handler = handler_type.__new__(handler_type)
    handler.path = "/api/v1/runs/example?mutation=1"
    handler.headers = {
        "Origin": "",
        "Host": "localhost",
        "Sec-Fetch-Site": "same-origin",
        "Content-Type": "application/json",
    }
    response = []
    handler._json_response = lambda status, document: response.append((status, document))
    handler.do_POST()
    assert response == [(405, {"error": "inspector API is read-only"})]

    static_response = handler_type.__new__(handler_type)
    static_response.send_response = lambda status: response.append((status, "static"))
    static_response.send_header = lambda *_args: None
    static_response.end_headers = lambda: None
    static_response.wfile = BytesIO()
    static_response._static_response("/admin")
    static_html = static_response.wfile.getvalue()
    assert b'data-mode="admin"' in static_html
    assert b'id="run-select"' in static_html
