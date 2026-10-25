"""Toilets installed by an intervention are placed on the floor plan and announced."""

from __future__ import annotations

import json
import math

import pytest

from sheltersim.contracts.extensions import point_in_polygon
from sheltersim.core.world import WorldState
from sheltersim.domain.agent_dynamics import (
    AGENT_DYNAMICS_STATE_KEY,
    ANNOUNCED_FACILITY_AGENT_DYNAMICS_POLICY_VERSION,
    INSTALLED_FACILITY_ANNOUNCEMENT_SOURCE_ID,
    ORIENTATION_AGENT_DYNAMICS_POLICY_VERSION,
    canonical_agent_dynamics_config,
)
from sheltersim.domain.geometry_overlay import (
    GEOMETRY_OVERLAY_KEY,
    INSTALLED_FACILITY_MIN_SEPARATION_M,
    INSTALLED_FACILITY_PLACEMENT_SOURCE_ID,
    effective_geometry,
)
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.events.effects import (
    EffectProgram,
    SupportMode,
    TargetSelector,
    TimedAction,
    validate_effect_program,
)
from sheltersim.events.models import EventStatus
from sheltersim.experiments.branch import AddToiletCommand, BranchError, BranchRuntime
from sheltersim.experiments.initial_scenario import InitialScenarioBuilder, InitialScenarioRequest
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.storage.projects import ProjectStore, digest

_FORK_MINUTE = 30


def _generated(
    *, policy_version: str | None = None, overlay_version: int | None = None
) -> ScenarioSpec:
    scenario = InitialScenarioBuilder().build(InitialScenarioRequest(
        municipality="Synthetic municipality", disaster="earthquake",
        evacuees=20, duration_days=0.25, seed=0,
    )).scenario
    if policy_version is None and overlay_version is None:
        return scenario
    document = scenario.model_dump(mode="json")
    if policy_version is not None:
        document["model_configs"]["agent_dynamics"] = canonical_agent_dynamics_config(
            {"policy_version": policy_version}
        )
    if overlay_version is not None:
        document["model_configs"]["geometry_overlay"] = {"schema_version": overlay_version}
    return ScenarioSpec.model_validate(document)


def _add_toilets(count: int = 2, *, start_minute: int = _FORK_MINUTE) -> EffectProgram:
    return EffectProgram(
        program_id="program-installed-toilets",
        narrative="Add temporary toilets",
        source_text="仮設トイレを追加",
        simulation_start_minute=start_minute,
        effects=[TimedAction(
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
        )],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )


def _timeline(tmp_path, scenario: ScenarioSpec) -> TimelineStore:
    store = ProjectStore(tmp_path)
    store.create_project("p")
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "r", scenario.id, 1)
    timeline = TimelineStore(store, "p", "r")
    timeline.record(BranchRuntime.from_world(WorldState.from_scenario(scenario)))
    timeline.advance("main", _FORK_MINUTE)
    return timeline


def _forked_child(tmp_path, scenario: ScenarioSpec, until: int) -> BranchRuntime:
    timeline = _timeline(tmp_path, scenario)
    program = _add_toilets()
    timeline.fork("main", _FORK_MINUTE, "child", program=program,
                  approved_program_hash=digest(program.model_dump(mode="json")))
    return timeline.advance("child", until)


def _world(scenario: ScenarioSpec) -> WorldState:
    world = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(world)
    return world


def _added_toilets(world: WorldState) -> set[str]:
    return {key for key in world.facilities if key.startswith("effect-toilet-")}


def _knowers(world: WorldState, facility_id: str) -> int:
    agents = world.model_state[AGENT_DYNAMICS_STATE_KEY]["agents"].values()
    return sum(facility_id in record["known_facility_ids"] for record in agents)


def test_generated_scenarios_announce_and_place_installed_facilities() -> None:
    scenario = _generated()
    assert scenario.model_configs["agent_dynamics"]["policy_version"] == (
        ANNOUNCED_FACILITY_AGENT_DYNAMICS_POLICY_VERSION
    )
    assert scenario.model_configs["geometry_overlay"] == {"schema_version": 4}
    manifest = json.loads(open("fixtures/source-manifest.json", encoding="utf-8").read())
    for source_id in (INSTALLED_FACILITY_ANNOUNCEMENT_SOURCE_ID,
                      INSTALLED_FACILITY_PLACEMENT_SOURCE_ID):
        assert any(row["id"] == source_id for row in scenario.provenance["assumptions"])
        assert any(row["id"] == source_id for row in manifest["assumptions"])


def test_installed_toilets_are_placed_announced_and_used(tmp_path) -> None:
    before = effective_geometry(_world(_generated())).facility_points
    child = _forked_child(tmp_path, _generated(), until=180)
    world = child.world
    added = _added_toilets(world)
    assert len(added) == 2
    geometry = effective_geometry(world)
    zone = next(row for row in geometry.zones if row.zone_id == "toilet")
    for facility_id in added:
        point = geometry.facility_points[facility_id]
        assert point_in_polygon(point, zone.polygon)
        assert all(
            math.hypot(point.x - other.x, point.y - other.y) >= INSTALLED_FACILITY_MIN_SEPARATION_M
            for key, other in geometry.facility_points.items()
            if key != facility_id and world.facilities[key].zone_id == "toilet"
        )
        record = world.model_state[GEOMETRY_OVERLAY_KEY]["facility_points"][facility_id]
        assert INSTALLED_FACILITY_PLACEMENT_SOURCE_ID in record["source_ids"]
        assert _knowers(world, facility_id) == len(world.agents)
    # Existing facilities keep the points the generator gave them.
    assert all(geometry.facility_points[key] == point for key, point in before.items())
    served = {
        record.event.payload["facility_id"] for record in world.event_log.records
        if record.event.event_type == "toilet_service_complete"
        and record.status is EventStatus.APPLIED
    }
    assert served & added, "no Agent was served at an installed toilet"


def test_v2_histories_keep_installed_facilities_unknown(tmp_path) -> None:
    # Saved v2 Runs must replay exactly, so v2 keeps the earlier behaviour.
    child = _forked_child(
        tmp_path, _generated(policy_version=ORIENTATION_AGENT_DYNAMICS_POLICY_VERSION), until=60
    )
    for facility_id in _added_toilets(child.world):
        assert _knowers(child.world, facility_id) == 0


def test_floor_plans_without_placement_report_add_toilet_as_not_applicable() -> None:
    world = _world(_generated(overlay_version=3))
    issues = validate_effect_program(world, _add_toilets(start_minute=0))
    assert any("cannot place added toilets on this floor plan" in issue for issue in issues)


def test_add_toilet_is_not_applicable_when_the_zone_has_no_free_point() -> None:
    # The 20-person toilet zone is 1.8 m wide; four more toilets fit, a fifth does not.
    world = _world(_generated())
    assert validate_effect_program(world, _add_toilets(4, start_minute=0)) == ()
    issues = validate_effect_program(world, _add_toilets(5, start_minute=0))
    assert any("no free floor-plan point for toilet 5" in issue for issue in issues)


def test_runtime_placement_failure_fails_the_event_without_changes(tmp_path, monkeypatch) -> None:
    timeline = _timeline(tmp_path, _generated())
    program = _add_toilets()
    timeline.fork("main", _FORK_MINUTE, "child", program=program,
                  approved_program_hash=digest(program.model_dump(mode="json")))
    # Something placed later (a partition, a room) can take the free point.
    import sheltersim.domain.geometry_overlay as overlay
    monkeypatch.setattr(overlay, "installed_facility_point", lambda world, facility_id: None)
    world = timeline.advance("child", _FORK_MINUTE + 1).world
    assert not _added_toilets(world)
    failed = [record for record in world.event_log.records
              if record.event.event_type == "effect_program_structure"]
    assert [record.status for record in failed] == [EventStatus.FAILED]


def test_branch_add_toilet_command_places_and_announces_the_new_toilet() -> None:
    runtime = BranchRuntime.from_world(_world(_generated()))
    runtime.apply(AddToiletCommand(facility_id="extra_toilet", zone_id="toilet"))
    assert "extra_toilet" in effective_geometry(runtime.world).facility_points
    assert _knowers(runtime.world, "extra_toilet") == len(runtime.world.agents)


def test_branch_add_toilet_command_refuses_a_floor_plan_without_placement() -> None:
    runtime = BranchRuntime.from_world(_world(_generated(overlay_version=3)))
    with pytest.raises(BranchError, match="cannot place added toilets"):
        runtime.apply(AddToiletCommand(facility_id="extra_toilet", zone_id="toilet"))
    assert "extra_toilet" not in runtime.world.facilities
