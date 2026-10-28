"""Saved-run web inspector queries: profiles, agents, events, KPIs and the map, with
bounded filters and Run IDs that cannot leave the runs folder. A generated Scenario's
Run is mapped from its floor plan.

The allow-listed API dispatch, story cards and the read-only HTTP gate are checked in
tests/e2e/test_web_inspector_http.py.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.web.inspector import InspectorService, WebQueryError


def test_saved_run_supports_profiles_agent_events_kpis_and_synthetic_map(
    full_trace_inspector: InspectorService,
) -> None:
    run_id = full_trace_inspector.list_runs()["runs"][0]["id"]
    overview = full_trace_inspector.overview(run_id)
    profiles = full_trace_inspector.profiles(run_id, limit=100)
    agent_id = profiles["items"][0]["id"]
    agent = full_trace_inspector.agent(run_id, agent_id, at_minute=2)
    timeline = full_trace_inspector.timeline(run_id, limit=20)
    planned = full_trace_inspector.events(run_id, status="planned")
    realized = full_trace_inspector.events(run_id, status="realized")
    map_document = full_trace_inspector.zone_map(run_id, at_minute=2, selected_agent_id=agent_id)

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
    audit = full_trace_inspector.audit(run_id)
    assert audit["llm_calls"] == []
    assert "input_text" not in json.dumps(audit)


def test_query_filters_are_bounded_and_run_ids_cannot_escape_root(
    full_trace_inspector: InspectorService,
) -> None:
    run_id = full_trace_inspector.list_runs()["runs"][0]["id"]
    assert full_trace_inspector.profiles(run_id, query="A001", limit=100)["total"] == 1
    with pytest.raises(WebQueryError, match="limit"):
        full_trace_inspector.profiles(run_id, limit=101)
    with pytest.raises(WebQueryError, match="characters"):
        full_trace_inspector.overview("../outside")
    with pytest.raises(WebQueryError, match="does not exist"):
        full_trace_inspector.agent(run_id, "unknown-agent")
    with pytest.raises(WebQueryError, match="duration"):
        full_trace_inspector.zone_map(run_id, at_minute=99)


def _inside(x: float, y: float, polygon: list[list[float]]) -> bool:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        (x1, y1), (x2, y2) = current, previous
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
        previous = current
    return inside


def test_generated_scenario_run_is_mapped_from_its_floor_plan(
    tmp_path: Path, generated_previews
) -> None:
    """The bundled gym has no food, intake, medical, quiet or staff zone (#161)."""
    preview = generated_previews(duration_days=1 / 48)
    scenario = ScenarioSpec.model_validate(copy.deepcopy(preview["scenario"]))
    run_scenario(scenario, tmp_path / "gen", trace_level=TraceLevel.FULL)

    map_data = InspectorService(tmp_path).zone_map("gen", at_minute=20)

    assert map_data["layout"]["id"] == "facility-geometry"
    polygons = {zone["id"]: zone["polygon"] for zone in map_data["zones"]}
    assert set(polygons) == {zone.id for zone in scenario.zones}
    assert all(zone.get("modelled", True) for zone in map_data["zones"])
    assert map_data["undrawn_agents"] == []
    assert len(map_data["agents"]) == len(scenario.profiles)
    assert all(
        _inside(agent["x"], agent["y"], polygons[agent["zone_id"]]) for agent in map_data["agents"]
    )
    assert {item["id"] for item in map_data["facilities"]} == set(
        scenario.model_configs["facility_geometry"]["facility_points"]
    )
