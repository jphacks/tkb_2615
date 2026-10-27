"""Water and meals served through the tick order: travel, queue, staffed service and
completion, with the same outputs at every trace level.

On the shared generated Scenario, so the person walks on the floor plan to the
station's area and then to its service point, and a worker with the right skill
serves them. Until #137 this ran on h00_small_replay with a water station and meal
point added by hand, with neither a service point nor a walk inside an area.

The service's requests, reservations, cancellations and checks are tested in
tests/unit/domain/test_resource_service.py.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.domain.resources import RESOURCE_LEDGER_KEY
from sheltersim.events.models import EventLogRecord
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.inspection.traces import RunArtifactWriter

# A001's next decision falls at minute 15, so each run serves A001 once.
MINUTES = 14


def _quiet_world(scenario, *, hydration: float = 0.1, hunger: float = 0.1) -> WorldState:
    """Only A001 decides, at minute 0, with the given needs; nobody else needs anything."""
    scenario.model_configs["behavior"]["temperature"] = 0.0001
    world = WorldState.from_scenario(scenario)
    for agent in world.agents.values():
        agent.next_decision_at = 100
        agent.needs.toilet_urgency = 0.0
        agent.needs.hunger = 0.1
        agent.needs.fatigue = 0.1
        agent.needs.hydration_need = 0.1
        agent.stress = 0.0
    agent = world.agents["A001"]
    agent.profile = replace(agent.profile, support_need=0.0)
    agent.needs.hydration_need = hydration
    agent.needs.hunger = hunger
    agent.next_decision_at = 0
    return world


def _run(scenario, world: WorldState, output: Path, trace_level: TraceLevel) -> None:
    artifacts = RunArtifactWriter(scenario, output, trace_level=trace_level)
    artifacts.write_initial(world)
    TickEngine(world, phase_handler=OfflineSimulationController(world, artifacts)).run(
        steps=MINUTES
    )
    artifacts.finalize(world)


def _events(world: WorldState, event_type: str) -> list[EventLogRecord]:
    return [
        record
        for record in world.event_log.records
        if record.event.event_type == event_type and record.event.payload.get("agent_id") == "A001"
    ]


def _decisions(output: Path) -> list[dict]:
    lines = (output / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.parametrize(
    "starts_in_station_area", [False, True], ids=["from_sleeping_area", "in_station_area"]
)
def test_behavior_fetch_water_runs_travel_queue_service_completion_and_artifacts(
    tmp_path: Path, starts_in_station_area: bool, generated_scenario, place_three_at_facility
) -> None:
    scenario = generated_scenario
    config = scenario.model_configs["resource_service"]
    world = _quiet_world(scenario, hydration=0.9)
    station = world.facilities["water_station_main"]
    if starts_in_station_area:
        place_three_at_facility(world, station.id)
        # The placement holds everyone's decisions, A001's too.
        world.agents["A001"].next_decision_at = 0

    output = tmp_path / "water"
    _run(scenario, world, output, TraceLevel.FULL)

    travel = [
        record.event.payload
        for record in _events(world, "agent_travel_complete")
        if record.event.payload["purpose"] == "resource_request"
    ]
    between_areas = [trip for trip in travel if trip["origin_zone_id"] != station.zone_id]
    within_area = [trip for trip in travel if trip["origin_zone_id"] == station.zone_id]
    # From the sleeping area the route passes the corridor; either way, the last
    # stretch is a walk inside the station's area that the fixture never had.
    assert bool(between_areas) is not starts_in_station_area
    assert all(trip["route_zone_ids"][-1] == station.zone_id for trip in between_areas)
    assert within_area
    (reserve,) = _events(world, "resource_reserve")
    (complete,) = _events(world, "resource_service_complete")
    assert complete.event.payload["facility_id"] == station.id
    assert complete.realized_minute - reserve.realized_minute == config["service_minutes"]
    assert config["water_staff_skill"] in world.staff[complete.event.payload["staff_id"]].skills

    other = world.agents["A002"].needs.hydration_need - 0.1
    assert world.agents["A001"].needs.hydration_need == pytest.approx(
        0.9 - config["hydration_relief"] + other
    )
    water = world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]
    assert water["served"] == config["water_ml_per_service"]
    assert water["reserved"] == 0
    assert any(
        record["agent_id"] == "A001" and record["action"] == "fetch_water"
        for record in _decisions(output)
    )
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["world_state_schema_version"] == 6
    assert manifest["resource_service_summary"]["policy_version"] == config["policy_version"]
    assert manifest["model_assumptions"]["resource_service"]["source_id"] == config["source_id"]
    assert (
        json.loads((output / "resource_service.json").read_text(encoding="utf-8"))["schema_version"]
        == 1
    )


def test_behavior_collect_meal_consumes_food_and_reduces_hunger(
    tmp_path: Path, generated_scenario
) -> None:
    scenario = generated_scenario
    config = scenario.model_configs["resource_service"]
    world = _quiet_world(scenario, hunger=0.95)
    food_before = world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["food_meal_units"]["remaining"]

    output = tmp_path / "meal-service"
    _run(scenario, world, output, TraceLevel.FULL)

    (complete,) = _events(world, "resource_service_complete")
    assert complete.event.payload["facility_id"] == "meal_distribution_main"
    assert config["meal_staff_skill"] in world.staff[complete.event.payload["staff_id"]].skills
    food = world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["food_meal_units"]
    assert food["served"] == config["food_meal_units_per_service"]
    assert food["remaining"] == food_before - config["food_meal_units_per_service"]
    assert food["reserved"] == 0
    other = world.agents["A002"].needs.hunger - 0.1
    assert world.agents["A001"].needs.hunger == pytest.approx(
        0.95 - config["hunger_relief"] + other
    )
    assert world.model_state[RESOURCE_LEDGER_KEY]["audit"][-1]["operation"] == "consume"
    assert any(
        record["agent_id"] == "A001" and record["action"] == "collect_meal"
        for record in _decisions(output)
    )


def test_resource_service_trace_off_and_summary_have_identical_outputs(
    tmp_path: Path, generated_scenario_with, place_three_at_facility
) -> None:
    outputs: dict[TraceLevel, tuple[WorldState, Path]] = {}
    for trace_level in (TraceLevel.OFF, TraceLevel.SUMMARY):
        scenario = generated_scenario_with()
        world = _quiet_world(scenario, hydration=0.9)
        place_three_at_facility(world, "water_station_main")
        world.agents["A001"].next_decision_at = 0
        output = tmp_path / trace_level.value
        _run(scenario, world, output, trace_level)
        assert _events(world, "resource_service_complete")
        outputs[trace_level] = (world, output)

    off_world, off_path = outputs[TraceLevel.OFF]
    summary_world, summary_path = outputs[TraceLevel.SUMMARY]
    assert off_world.canonical_json() == summary_world.canonical_json()
    assert off_world.event_log.records == summary_world.event_log.records
    assert (off_path / "events_realized.jsonl").read_bytes() == (
        summary_path / "events_realized.jsonl"
    ).read_bytes()
    assert (off_path / "metrics.csv").read_bytes() == (summary_path / "metrics.csv").read_bytes()
    for artifact_name in ("resource_ledger.json", "resource_service.json"):
        assert (off_path / artifact_name).read_bytes() == (
            summary_path / artifact_name
        ).read_bytes()
