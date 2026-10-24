"""Independent service-point FIFO execution and checkpoint evidence."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from sheltersim.contracts.extensions import FacilityGeometry
from sheltersim.contracts.workspace import HistoryQuery
from sheltersim.core.checkpoint import Checkpoint, _digest
from sheltersim.core.engine import TickEngine
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.facilities import ToiletServiceManager
from sheltersim.domain.facility_queues import FACILITY_QUEUES_STATE_KEY
from sheltersim.domain.movement import MOVEMENT_STATE_KEY
from sheltersim.events.models import EventStatus
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.frames import world_frame
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.storage.history_index import HistoryIndex
from sheltersim.storage.projects import ProjectStore

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenarios" / "h00_small_replay.json"


def multi_queue_scenario() -> ScenarioSpec:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["facilities"].extend([
        {"id": "water_station_1", "kind": "water_station", "zone_id": "toilet",
         "capacity": 2, "working_units": 2},
        {"id": "meal_distribution_1", "kind": "meal_distribution", "zone_id": "toilet",
         "capacity": 1, "working_units": 1},
    ])
    document["staff"].extend([
        {"id": "S-WATER-1", "zone_id": "toilet", "skills": ["water_service"]},
        {"id": "S-WATER-2", "zone_id": "toilet", "skills": ["water_service"]},
        {"id": "S-TOILET-1", "zone_id": "toilet", "skills": ["toilet_service"]},
    ])
    document["initial_stock"] = {
        "schema_version": 1, "warehouse_id": "queue-test-warehouse",
        "zone_id": "toilet", "water_ml": 1000, "food_meal_units": 2,
        "source_ids": ["ASM-QUEUE-TEST-STOCK-V1"],
    }
    document["model_configs"]["resource_service"] = {
        "schema_version": 1, "policy_version": "resource-service-v1",
        "source_id": "ASM-QUEUE-TEST-RESOURCE-V1", "source_type": "assumption",
        "service_minutes": 2, "water_ml_per_service": 250,
        "food_meal_units_per_service": 1, "hydration_growth_per_minute": 0.02,
        "hydration_action_threshold": 0.25, "hunger_action_threshold": 0.7,
        "hydration_relief": 0.35, "hunger_relief": 0.5,
        "water_staff_skill": "water_service",
    }
    zone_ids = [row["id"] for row in document["zones"]]
    rectangle = [
        {"x": 0.0, "y": 0.0}, {"x": 10.0, "y": 0.0},
        {"x": 10.0, "y": 10.0}, {"x": 0.0, "y": 10.0},
    ]
    points = {
        facility["id"]: {"x": 7.0, "y": 5.0}
        for facility in document["facilities"]
    }
    routes = [
        {"edge_id": edge["id"], "points": [{"x": 3.0, "y": 5.0}, {"x": 6.0, "y": 5.0}],
         "width_m": 2.0, "accessible": True}
        for edge in document["zone_edges"]
    ]
    document["model_configs"]["facility_geometry"] = {
        "schema_version": 1, "units": "m", "origin": "top_left",
        "width_m": 10.0, "height_m": 10.0, "source_kind": "synthetic",
        "zones": [
            {"zone_id": zone_id, "polygon": rectangle, "usable_area_m2": 100.0}
            for zone_id in zone_ids
        ],
        "facility_points": points,
        "routes": routes,
        "service_points": [
            {
                "schema_version": 1, "id": "toilet-1-left", "facility_id": "toilet_1",
                "point_m": {"x": 8.0, "y": 4.0}, "service_capacity": 1,
                "queue_id": "toilet-1-left-fifo",
                "access": {
                    "accessibility": "accessible", "required_staff_skills": ["toilet_service"],
                },
            },
            {
                "schema_version": 1, "id": "toilet-1-right", "facility_id": "toilet_1",
                "point_m": {"x": 8.0, "y": 6.0}, "service_capacity": 1,
                "queue_id": "toilet-1-right-fifo",
                "access": {
                    "accessibility": "accessible", "required_staff_skills": ["toilet_service"],
                },
            },
            {
                "schema_version": 1, "id": "water-1-left", "facility_id": "water_station_1",
                "point_m": {"x": 8.0, "y": 4.0}, "service_capacity": 1,
                "queue_id": "water-1-left-fifo",
                "access": {"accessibility": "accessible", "required_staff_skills": ["water_service"]},
            },
            {
                "schema_version": 1, "id": "water-1-right", "facility_id": "water_station_1",
                "point_m": {"x": 8.0, "y": 6.0}, "service_capacity": 1,
                "queue_id": "water-1-right-fifo",
                "access": {"accessibility": "accessible", "required_staff_skills": ["water_service"]},
            },
        ],
    }
    FacilityGeometry.model_validate(document["model_configs"]["facility_geometry"])
    document["model_configs"]["toilet_service"].update(
        {"minimum_minutes": 2, "maximum_minutes": 2}
    )
    document["duration_minutes"] = 18
    return ScenarioSpec.model_validate(document)


def prepare_queued_agents(world: WorldState) -> None:
    for agent_id in ("A001", "A002", "A003"):
        world.agents[agent_id].zone_id = "toilet"
        world.agents[agent_id].next_decision_at = 100
        world.model_state[MOVEMENT_STATE_KEY]["positions"][agent_id].update(
            zone_id="toilet", point={"x": 5.0, "y": 5.0}, edge_id=None,
            progress=0.0, position_kind="physical",
        )


def test_explicit_queues_run_independently_and_restore_from_checkpoint(tmp_path: Path) -> None:
    scenario = multi_queue_scenario()
    world = WorldState.from_scenario(scenario)
    prepare_queued_agents(world)
    service = ToiletServiceManager(world, min_service_minutes=2, max_service_minutes=2)
    service.request("A001", "toilet_1", queue_id="toilet-1-left-fifo")
    service.request("A002", "toilet_1", queue_id="toilet-1-left-fifo")
    service.request("A003", "toilet_1", queue_id="toilet-1-right-fifo")

    state = world.model_state[FACILITY_QUEUES_STATE_KEY]
    assert len(state["queues"]["toilet-1-left-fifo"]["queued_request_ids"]) == 2
    assert len(state["queues"]["toilet-1-right-fifo"]["queued_request_ids"]) == 1
    assert all(
        state["requests"][request_id]["queue_id"] == queue_id
        for queue_id, queue in state["queues"].items()
        for request_id in queue["queued_request_ids"]
    )
    world.validate()
    frame = world_frame(world, run_id="run", branch_id="branch", computed_until=0)
    left = next(
        item for item in frame["facilities"] if item["id"] == "toilet_1"
    )["service_queues"]
    assert {item["queue_id"]: len(item["queue_agent_ids"]) for item in left} == {
        "toilet-1-left-fifo": 2,
        "toilet-1-right-fifo": 1,
    }
    restored = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    assert restored.canonical_json() == world.canonical_json()

    output = tmp_path / "independent-queues"
    artifacts = RunArtifactWriter(scenario, output)
    artifacts.write_initial(world)
    controller = OfflineSimulationController(world, artifacts)
    engine = TickEngine(world, phase_handler=controller, rng=CounterRNG(world.seed))
    engine.step()
    in_flight = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    assert in_flight.canonical_json() == world.canonical_json()
    assert in_flight.model_state[MOVEMENT_STATE_KEY]["journeys"]
    engine.run(15)
    artifacts.finalize(world)

    transitions = [
        record.event.payload
        for record in world.event_log.records
        if record.event.event_type == "facility_queue_transition"
        and record.status is EventStatus.APPLIED
    ]
    assert sum(row["transition"] == "completed" for row in transitions) == 3
    toilet_completions = [
        record.event.payload for record in world.event_log.records
        if record.event.event_type == "toilet_service_complete"
    ]
    assert len(toilet_completions) == 3
    assert all(row["staff_id"] == "S-TOILET-1" for row in toilet_completions)
    assert world.staff["S-TOILET-1"].active_task_id is None
    assert all(not row["queued_request_ids"] and not row["active_request_ids"] for row in state["queues"].values())
    assert world.metric_state["toilet_service_completed_count"] == 3.0
    world.validate()
    store = ProjectStore(tmp_path / "indexed-project")
    store.create_project("p")
    store._path("p", "runs", "r").mkdir(parents=True)
    checkpoint = Checkpoint.capture(world)
    index = HistoryIndex(store, "p", "r")
    index.ingest("branch", checkpoint, rebuild=True)
    history = index.search(
        "branch",
        HistoryQuery(kind="operations", start=0, end=16, limit=10,
                     event_type="facility_queue_transition"),
        checkpoint.checkpoint_hash,
    )
    assert len(history["items"]) == 9
    assert {row["transition"] for row in history["items"]} == {
        "queued", "service_started", "completed"
    }


def test_independent_resource_queues_reserve_staff_and_consume_stock(tmp_path: Path) -> None:
    from sheltersim.domain.resource_service import ResourceServiceManager
    from sheltersim.domain.resources import RESOURCE_LEDGER_KEY

    scenario = multi_queue_scenario()
    world = WorldState.from_scenario(scenario)
    prepare_queued_agents(world)
    service = ResourceServiceManager(world)
    for agent_id, queue_id in (
        ("A001", "water-1-left-fifo"),
        ("A002", "water-1-left-fifo"),
        ("A003", "water-1-right-fifo"),
    ):
        service.request(agent_id, "water_station_1", queue_id=queue_id)

    output = tmp_path / "independent-resource-queues"
    artifacts = RunArtifactWriter(scenario, output)
    artifacts.write_initial(world)
    controller = OfflineSimulationController(world, artifacts)
    TickEngine(world, phase_handler=controller, rng=CounterRNG(world.seed)).run(16)
    artifacts.finalize(world)

    transitions = [
        record.event.payload
        for record in world.event_log.records
        if record.event.event_type == "facility_queue_transition"
        and record.status is EventStatus.APPLIED
        and record.event.payload["service_kind"] == "water"
    ]
    assert sum(row["transition"] == "completed" for row in transitions) == 3
    assert world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]["served"] == 750
    assert not world.facilities["water_station_1"].resource_queue_agent_ids
    assert not world.facilities["water_station_1"].resource_active_agent_ids
    world.validate()


def test_prequeue_checkpoint_keeps_legacy_fifo_mode_and_can_be_saved_again() -> None:
    from sheltersim.domain.facility_queues import (
        FACILITY_QUEUES_LEGACY_MODE_KEY,
        FACILITY_QUEUES_STATE_KEY,
    )

    world = WorldState.from_scenario(multi_queue_scenario())
    document = Checkpoint.capture(world).to_dict()
    legacy_world = deepcopy(document["world_state"])
    legacy_world["model_state"].pop(FACILITY_QUEUES_STATE_KEY)
    document["world_state"] = legacy_world
    document["state_hash"] = _digest(legacy_world)
    document["model_versions"].pop("simulator:facility_queues_v1")
    document.pop("checkpoint_hash")
    document["checkpoint_hash"] = _digest(document)

    restored = Checkpoint.from_dict(document).restore_world()
    assert restored.model_state[FACILITY_QUEUES_LEGACY_MODE_KEY]["mode"] == (
        "pre_independent_queue_checkpoint"
    )
    assert FACILITY_QUEUES_STATE_KEY not in restored.model_state
    new_checkpoint = Checkpoint.capture(restored)
    reloaded = Checkpoint.from_json(new_checkpoint.to_json()).restore_world()
    assert FACILITY_QUEUES_STATE_KEY not in reloaded.model_state
