"""Toilets installed by an intervention on a Branch are placed, announced and used.

Whether an add-toilet order can be placed at all, and the Branch command that adds
one directly, are checked in tests/unit/domain/test_geometry_overlay.py.
"""

from __future__ import annotations

import pytest

import math

from sheltersim.contracts.extensions import point_in_polygon
from sheltersim.core.world import WorldState
from sheltersim.domain.agent_dynamics import (
    AGENT_DYNAMICS_STATE_KEY,
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
from sheltersim.events.effects import EffectProgram
from sheltersim.events.models import EventStatus
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.storage.projects import digest

_FORK_MINUTE = 30


def _timeline(saved_timeline, tmp_path, scenario: ScenarioSpec) -> TimelineStore:
    """A recorded Run of scenario, computed to the fork minute.

    saved_timeline is the fixture of the same name, passed on by the calling test.
    """
    _, timeline = saved_timeline(tmp_path, scenario)
    timeline.advance("main", _FORK_MINUTE)
    return timeline


def _forked_child(
    saved_timeline, tmp_path, scenario: ScenarioSpec, until: int, program: EffectProgram
) -> BranchRuntime:
    timeline = _timeline(saved_timeline, tmp_path, scenario)
    timeline.fork(
        "main",
        _FORK_MINUTE,
        "child",
        program=program,
        approved_program_hash=digest(program.model_dump(mode="json")),
    )
    return timeline.advance("child", until)


def _with_agent_dynamics(scenario: ScenarioSpec, policy_version: str) -> ScenarioSpec:
    document = scenario.model_dump(mode="json")
    document["model_configs"]["agent_dynamics"] = canonical_agent_dynamics_config(
        {"policy_version": policy_version}
    )
    return ScenarioSpec.model_validate(document)


def _world(scenario: ScenarioSpec) -> WorldState:
    world = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(world)
    return world


def _added_toilets(world: WorldState) -> set[str]:
    return {key for key in world.facilities if key.startswith("effect-toilet-")}


def _knowers(world: WorldState, facility_id: str) -> int:
    agents = world.model_state[AGENT_DYNAMICS_STATE_KEY]["agents"].values()
    return sum(facility_id in record["known_facility_ids"] for record in agents)


def test_installed_toilets_are_placed_announced_and_used(
    tmp_path, generated_scenario, add_toilets_program, saved_timeline
) -> None:
    before = effective_geometry(_world(generated_scenario)).facility_points
    child = _forked_child(
        saved_timeline,
        tmp_path,
        generated_scenario,
        until=180,
        program=add_toilets_program(start_minute=_FORK_MINUTE),
    )
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
        record.event.payload["facility_id"]
        for record in world.event_log.records
        if record.event.event_type == "toilet_service_complete"
        and record.status is EventStatus.APPLIED
    }
    assert served & added, "no Agent was served at an installed toilet"


def test_v2_histories_keep_installed_facilities_unknown(
    tmp_path, generated_scenario, add_toilets_program, saved_timeline
) -> None:
    # Saved v2 Runs must replay exactly, so v2 keeps the earlier behaviour.
    child = _forked_child(
        saved_timeline,
        tmp_path,
        _with_agent_dynamics(generated_scenario, ORIENTATION_AGENT_DYNAMICS_POLICY_VERSION),
        until=60,
        program=add_toilets_program(start_minute=_FORK_MINUTE),
    )
    for facility_id in _added_toilets(child.world):
        assert _knowers(child.world, facility_id) == 0


@pytest.mark.python_rules
def test_runtime_placement_failure_fails_the_event_without_changes(
    tmp_path, monkeypatch, generated_scenario, add_toilets_program, saved_timeline
) -> None:
    timeline = _timeline(saved_timeline, tmp_path, generated_scenario)
    program = add_toilets_program(start_minute=_FORK_MINUTE)
    timeline.fork(
        "main",
        _FORK_MINUTE,
        "child",
        program=program,
        approved_program_hash=digest(program.model_dump(mode="json")),
    )
    # Something placed later (a partition, a room) can take the free point. This is the Python
    # rule's text; tests/integration/test_overlay_rules.py has the kernel refusing the same.
    import sheltersim.domain.geometry_overlay as overlay
    from sheltersim.events import effects

    monkeypatch.setattr(overlay, "installed_facility_point", lambda world, facility_id: None)
    monkeypatch.setattr(
        effects, "place_installed_facility", overlay.place_installed_facility.__wrapped__
    )
    monkeypatch.setattr(
        effects.EffectProgramStructureHandler,
        "apply",
        effects.EffectProgramStructureHandler.apply.__wrapped__,
    )
    world = timeline.advance("child", _FORK_MINUTE + 1).world
    assert not _added_toilets(world)
    failed = [
        record
        for record in world.event_log.records
        if record.event.event_type == "effect_program_structure"
    ]
    assert [record.status for record in failed] == [EventStatus.FAILED]
