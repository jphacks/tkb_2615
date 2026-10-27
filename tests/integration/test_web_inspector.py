"""Saved-run web inspector queries: profiles, agents, events, KPIs and the synthetic map,
with bounded filters and Run IDs that cannot leave the runs folder.

The allow-listed API dispatch, story cards and the read-only HTTP gate are checked in
tests/e2e/test_web_inspector_http.py.
"""

from __future__ import annotations

import json

import pytest

from sheltersim.web.inspector import InspectorService, WebQueryError


@pytest.fixture
def inspector(full_trace_inspector: InspectorService) -> InspectorService:
    return full_trace_inspector


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


