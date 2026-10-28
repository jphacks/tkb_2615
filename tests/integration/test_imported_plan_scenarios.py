"""A floor plan the operator imports on the Project screen becomes the Scenario's facility.

The operator uploads a drawing and traces it by hand on the extracted raster preview
(or uploads the same plan as GeoJSON), approves the Geometry, and names it in the
Scenario preview. The Scenario then uses the plan's zones, facilities and routes, and
its Run resumes from a Checkpoint to the same world. The plan is the synthetic
shelter of tests/support/plans.py.

How each plan input is rejected is checked in tests/unit/web/test_plan_services.py.
"""

from __future__ import annotations

import json

import pytest
from support.plans import shelter_plan_geojson, shelter_plan_png

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore

END = 120
FORK_MINUTE = 60


def _approved_scenario(api, upload_plan, generated_request, plan: str, trace: dict) -> dict:
    """Import the plan as the Project screen does and return the Scenario preview."""
    if plan == "image":
        asset = upload_plan(api, shelter_plan_png())
        extracted = api.post(["project", "assets", asset, "plan-preview"], {"operation": "extract"})
        body = {
            "operation": "trace",
            "trace": trace,
            "page_index": 0,
            "raster_preview_id": extracted["preview_id"],
        }
    else:
        content = json.dumps(shelter_plan_geojson()).encode()
        asset = upload_plan(api, content, "application/geo+json")
        body = {"operation": "geojson"}
    preview = api.post(["project", "assets", asset, "plan-preview"], body)
    assert preview["kind"] == "geometry"
    api.post(
        ["project", "geometries", "plan"],
        {
            "preview_id": preview["preview_id"],
            "asset_id": asset,
            "approved_hash": preview["value_hash"],
            "approved": True,
        },
    )
    return api.workspace.preview(
        "project", {"text": "", "request": generated_request(), "geometry_id": "plan"}
    )


@pytest.mark.parametrize("plan", ["image", "geojson"])
def test_an_imported_plan_is_the_facility_of_a_startable_scenario_that_resumes(
    tmp_path, ui_project_api, upload_plan, generated_request, saved_timeline, shelter_trace, plan
):
    preview = _approved_scenario(
        ui_project_api, upload_plan, generated_request, plan, shelter_trace
    )

    assert preview["can_start"], preview["approval_blockers"]
    scenario = ScenarioSpec.model_validate(preview["scenario"])
    assert {zone.id for zone in scenario.zones} == {"sleeping", "food", "toilet"}
    assert {facility.id for facility in scenario.facilities} == {
        item["id"] for item in shelter_trace["facilities"]
    }
    geometry = scenario.model_configs["facility_geometry"]
    assert (geometry["width_m"], geometry["height_m"]) == (40, 30)
    assert geometry["facility_points"]["toilet_01"] == {"x": 26.0, "y": 25.0}

    store, timeline = saved_timeline(tmp_path / "projects", scenario)
    world = timeline.advance("main", END, checkpoint_interval=30).world
    assert {agent.zone_id for agent in world.agents.values()} <= {"sleeping", "food", "toilet"}
    assert len({agent.zone_id for agent in world.agents.values()}) > 1, "people moved"

    timeline.fork("main", FORK_MINUTE, "resumed")
    resumed = TimelineStore(store, "p", "r").advance("resumed", END, checkpoint_interval=END)
    assert json.dumps(resumed.world.to_dict(), sort_keys=True) == json.dumps(
        world.to_dict(), sort_keys=True
    )


def test_water_and_meals_are_served_on_an_imported_plan(
    tmp_path, ui_project_api, upload_plan, generated_request, saved_timeline, shelter_trace
):
    preview = _approved_scenario(
        ui_project_api, upload_plan, generated_request, "image", shelter_trace
    )
    if not preview["can_start"]:
        pytest.fail(f"the Scenario must start: {preview['approval_blockers']}")
    scenario = ScenarioSpec.model_validate(preview["scenario"])
    # Each of the plan's two stations gets a staff member in its zone, as in the standard
    # design, so neither service is reported as never starting.
    assert [member.zone_id for member in scenario.staff] == ["sleeping", "food", "food"]
    assert not any("一度も始まりません" in warning for warning in preview["warnings"])
    _, timeline = saved_timeline(tmp_path / "projects", scenario)
    initial = dict(timeline.seek("main", 0).world.resources)

    world = timeline.advance("main", scenario.duration_minutes).world

    assert world.resources["water_ml"] < initial["water_ml"]
    assert world.resources["food_meal_units"] < initial["food_meal_units"]
