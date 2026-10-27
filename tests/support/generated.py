"""The generated Scenario an operator makes on the Project screen, and that screen's API."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from support.isolation import _isolate

# The structured form the demo-v2 Scenario screen posts (scenarioRequest in
# demo-v2.js): only the fields an operator types, each marked as user input.
# Small enough that running the whole Scenario takes seconds.
GENERATED_SCENARIO_REQUEST: dict[str, Any] = {
    "municipality": "合成市",
    "disaster": "地震",
    "evacuees": 20,
    "duration_days": 0.25,
    "field_origins": {
        "municipality": "user",
        "disaster": "user",
        "evacuees": "user",
        "duration_days": "user",
    },
}


def _generated_request(**fields: Any) -> dict[str, Any]:
    """GENERATED_SCENARIO_REQUEST with some fields changed and marked as user input."""
    request = copy.deepcopy(GENERATED_SCENARIO_REQUEST)
    request.update(copy.deepcopy(fields))
    request["field_origins"] = {**request["field_origins"], **{name: "user" for name in fields}}
    return request


@contextmanager
def _ui_project_api(monkeypatch: pytest.MonkeyPatch, root: Path) -> Iterator[Any]:
    """The Project screen's API over a fresh store, with an empty regional research.

    The real research calls public web services, so it is replaced by an empty
    result; everything else goes through the same code as the UI.
    """
    from sheltersim.region.models import RegionResearchResult
    from sheltersim.storage.projects import ProjectStore
    from sheltersim.web.projects import ProjectAPI

    _isolate(monkeypatch, root)
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = ProjectStore(root / "store")
    store.create_project("project")
    api = ProjectAPI(store)
    api.workspace.research.research = lambda query, **_kwargs: RegionResearchResult(
        request=query, researched_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    try:
        yield api
    finally:
        api.close()


def _ui_preview(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Run the Project screen's Scenario preview for one structured form."""
    with pytest.MonkeyPatch.context() as monkeypatch, _ui_project_api(monkeypatch, root) as api:
        return api.workspace.preview("project", {"text": "", "request": request})


@pytest.fixture(scope="session")
def generated_previews(tmp_path_factory: pytest.TempPathFactory) -> Callable[..., dict[str, Any]]:
    """Previews of GENERATED_SCENARIO_REQUEST with some form fields changed.

    Each changed field is marked as user input, as the demo-v2 form does. Every
    distinct set of fields is built once per worker and shared, so do not change
    the returned preview.
    """
    cache: dict[str, dict[str, Any]] = {}

    def preview(**fields: Any) -> dict[str, Any]:
        key = json.dumps(fields, sort_keys=True, ensure_ascii=False)
        if key not in cache:
            cache[key] = _ui_preview(
                tmp_path_factory.mktemp("generated-scenario"), _generated_request(**fields)
            )
        return cache[key]

    return preview


@pytest.fixture(scope="session")
def generated_scenario_preview(generated_previews: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """The Scenario preview the Project screen gets for GENERATED_SCENARIO_REQUEST.

    It goes through the same ProjectAPI preview as the UI, so the Scenario has a
    floor plan (facility_geometry) and facility awareness (agent_dynamics) like
    every Scenario an operator creates. Only the regional research is replaced by
    an empty result, because the real one calls public web services.
    Built once per worker; use generated_scenario for a Scenario to change.
    """
    return generated_previews()


@pytest.fixture
def generated_scenario(generated_scenario_preview: dict[str, Any]):
    """A fresh ScenarioSpec of the shared generated preview, safe to change in one test."""
    from sheltersim.experiments.scenario import ScenarioSpec

    return ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))


@pytest.fixture
def generated_scenario_with(generated_previews: Callable[..., dict[str, Any]]):
    """Build a fresh ScenarioSpec from the shared form with some fields changed.

    For example generated_scenario_with(duration_days=4) is what an operator gets
    after typing 4 into the duration field.
    """
    from sheltersim.experiments.scenario import ScenarioSpec

    def build(**fields: Any):
        return ScenarioSpec.model_validate(copy.deepcopy(generated_previews(**fields)["scenario"]))

    return build


@pytest.fixture
def generated_request() -> Callable[..., dict[str, Any]]:
    """The structured form of GENERATED_SCENARIO_REQUEST with some fields changed."""
    return _generated_request


@pytest.fixture
def ui_project_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The Project screen's API with an empty project named "project", open for one test.

    Use it when a test must call further routes on the preview it creates, which
    the cached generated_previews cannot do. Build the form with generated_request.
    """
    with _ui_project_api(monkeypatch, tmp_path) as api:
        yield api


@pytest.fixture
def generated_scenario_with_split_queues(generated_scenario):
    """The generated Scenario with two service points, each its own queue, at two facilities.

    The generator gives every facility one service point. toilet_01 and
    water_station_main get a left and a right point here instead, one metre either
    side of the facility, so each serves two people at once, and each point needs a
    worker with the right skill; a toilet worker is added to the toilet area for that.
    """
    from sheltersim.experiments.scenario import ScenarioSpec

    document = generated_scenario.model_dump(mode="json")
    geometry = document["model_configs"]["facility_geometry"]
    zones = {facility["id"]: facility["zone_id"] for facility in document["facilities"]}
    facilities = {facility["id"]: facility for facility in document["facilities"]}
    for facility_id, skill in (
        ("toilet_01", "toilet_service"),
        ("water_station_main", "water_service"),
    ):
        facilities[facility_id]["capacity"] = facilities[facility_id]["working_units"] = 2
        point = geometry["facility_points"][facility_id]
        geometry["service_points"] = [
            row for row in geometry["service_points"] if row["facility_id"] != facility_id
        ]
        for side, offset in (("left", -1.0), ("right", 1.0)):
            geometry["service_points"].append(
                {
                    "schema_version": 1,
                    "id": f"{facility_id}-{side}",
                    "facility_id": facility_id,
                    "point_m": {"x": point["x"], "y": point["y"] + offset},
                    "service_capacity": 1,
                    "queue_id": f"{facility_id}-{side}-fifo",
                    "access": {"accessibility": "accessible", "required_staff_skills": [skill]},
                }
            )
    document["staff"].append(
        {"id": "toilet_staff_01", "zone_id": zones["toilet_01"], "skills": ["toilet_service"]}
    )
    return ScenarioSpec.model_validate(document)


@pytest.fixture
def place_three_at_facility() -> Callable[[Any, str], None]:
    """Stand A001-A003 in a facility's area and keep everyone from deciding anew.

    A request to a facility's queue needs its person to have arrived there.
    """

    def place(world: Any, facility_id: str) -> None:
        from sheltersim.domain.geometry_overlay import effective_geometry
        from sheltersim.domain.movement import MOVEMENT_STATE_KEY

        zone_id = world.facilities[facility_id].zone_id
        zone = next(row for row in effective_geometry(world).zones if row.zone_id == zone_id)
        centre = {
            "x": sum(point.x for point in zone.polygon) / len(zone.polygon),
            "y": sum(point.y for point in zone.polygon) / len(zone.polygon),
        }
        for agent in world.agents.values():
            agent.next_decision_at = 100
        for agent_id in ("A001", "A002", "A003"):
            world.agents[agent_id].zone_id = zone_id
            world.model_state[MOVEMENT_STATE_KEY]["positions"][agent_id].update(
                zone_id=zone_id,
                point=dict(centre),
                edge_id=None,
                progress=0.0,
                position_kind="physical",
            )

    return place


def _add_toilets_program(count: int = 2, *, start_minute: int) -> Any:
    from sheltersim.events.effects import EffectProgram, SupportMode, TargetSelector, TimedAction

    return EffectProgram(
        program_id="program-installed-toilets",
        narrative="Add temporary toilets",
        source_text="仮設トイレを追加",
        simulation_start_minute=start_minute,
        effects=[
            TimedAction(
                effect_id="add-toilets",
                op="add_toilet",
                target_selector=TargetSelector(kind="zone", target_id="toilet"),
                start_minute=start_minute,
                parameters={"count": count},
                units="toilet_stall",
                source_id="TEST-INSTALLED-FACILITY",
                assumption_ids=[],
                policy_dependencies=["facility_capacity"],
                reversible=False,
            )
        ],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )


@pytest.fixture(scope="session")
def add_toilets_program() -> Callable[..., Any]:
    """Build an EffectProgram that adds count toilets (2 by default) to the toilet zone.

    Session-scoped so module-scoped Runs can use it; each call builds a new program.
    """
    return _add_toilets_program
