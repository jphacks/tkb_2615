"""Independent service-point queues run, restore and feed the history index.

A checkpoint taken before these queues existed is checked in
tests/unit/domain/test_facility_queues.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sheltersim.contracts.workspace import HistoryQuery
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickEngine
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.facilities import ToiletServiceManager
from sheltersim.domain.facility_queues import FACILITY_QUEUES_STATE_KEY
from sheltersim.domain.movement import MOVEMENT_STATE_KEY
from sheltersim.domain.resource_service import ResourceServiceManager
from sheltersim.events.models import EventStatus
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import _MetricSink
from sheltersim.inspection.frames import world_frame
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.storage.history_index import HistoryIndex
from sheltersim.storage.projects import ProjectStore

QUEUED = ("A001", "A002", "A003")


def prepare_queued_agents(world: WorldState, facility_id: str) -> None:
    """Stand three people in the facility's area and keep everyone from deciding anew."""
    from sheltersim.domain.geometry_overlay import effective_geometry

    zone_id = world.facilities[facility_id].zone_id
    zone = next(row for row in effective_geometry(world).zones if row.zone_id == zone_id)
    centre = {
        "x": sum(point.x for point in zone.polygon) / len(zone.polygon),
        "y": sum(point.y for point in zone.polygon) / len(zone.polygon),
    }
    for agent in world.agents.values():
        agent.next_decision_at = 100
    for agent_id in QUEUED:
        world.agents[agent_id].zone_id = zone_id
        world.model_state[MOVEMENT_STATE_KEY]["positions"][agent_id].update(
            zone_id=zone_id,
            point=dict(centre),
            edge_id=None,
            progress=0.0,
            position_kind="physical",
        )


def test_explicit_queues_run_independently_and_restore_from_checkpoint(
    generated_scenario_with_split_queues, tmp_path: Path
) -> None:
    scenario = generated_scenario_with_split_queues
    world = WorldState.from_scenario(scenario)
    prepare_queued_agents(world, "toilet_01")
    service = ToiletServiceManager(world, min_service_minutes=2, max_service_minutes=2)
    service.request("A001", "toilet_01", queue_id="toilet_01-left-fifo")
    service.request("A002", "toilet_01", queue_id="toilet_01-left-fifo")
    service.request("A003", "toilet_01", queue_id="toilet_01-right-fifo")

    state = world.model_state[FACILITY_QUEUES_STATE_KEY]
    assert len(state["queues"]["toilet_01-left-fifo"]["queued_request_ids"]) == 2
    assert len(state["queues"]["toilet_01-right-fifo"]["queued_request_ids"]) == 1
    assert all(
        state["requests"][request_id]["queue_id"] == queue_id
        for queue_id, queue in state["queues"].items()
        for request_id in queue["queued_request_ids"]
    )
    world.validate()
    frame = world_frame(world, run_id="run", branch_id="branch", computed_until=0)
    left = next(item for item in frame["facilities"] if item["id"] == "toilet_01")["service_queues"]
    assert {item["queue_id"]: len(item["queue_agent_ids"]) for item in left} == {
        "toilet_01-left-fifo": 2,
        "toilet_01-right-fifo": 1,
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
        and record.event.payload["facility_id"] == "toilet_01"
    ]
    assert sum(row["transition"] == "completed" for row in transitions) == 3
    toilet_completions = [
        record.event.payload
        for record in world.event_log.records
        if record.event.event_type == "toilet_service_complete"
        and record.event.payload["facility_id"] == "toilet_01"
    ]
    assert sorted(row["agent_id"] for row in toilet_completions) == list(QUEUED)
    assert all(row["staff_id"] == "toilet_staff_01" for row in toilet_completions)
    assert world.staff["toilet_staff_01"].active_task_id is None
    assert all(
        not state["queues"][queue_id]["queued_request_ids"]
        and not state["queues"][queue_id]["active_request_ids"]
        for queue_id in ("toilet_01-left-fifo", "toilet_01-right-fifo")
    )
    world.validate()
    store = ProjectStore(tmp_path / "indexed-project")
    store.create_project("p")
    store._path("p", "runs", "r").mkdir(parents=True)
    checkpoint = Checkpoint.capture(world)
    index = HistoryIndex(store, "p", "r")
    index.ingest("branch", checkpoint, rebuild=True)
    history = index.search(
        "branch",
        HistoryQuery(
            kind="operations", start=0, end=16, limit=10, event_type="facility_queue_transition"
        ),
        checkpoint.checkpoint_hash,
    )
    toilet_history = [row for row in history["items"] if row["facility_id"] == "toilet_01"]
    assert len(toilet_history) == 9
    assert {row["transition"] for row in toilet_history} == {
        "queued",
        "service_started",
        "completed",
    }


def test_independent_resource_queues_reserve_staff_and_consume_stock(
    generated_scenario_with_split_queues, tmp_path: Path
) -> None:
    from sheltersim.domain.resources import RESOURCE_LEDGER_KEY

    scenario = generated_scenario_with_split_queues
    world = WorldState.from_scenario(scenario)
    prepare_queued_agents(world, "water_station_main")
    served_before = world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]["served"]
    per_service = world.model_configs["resource_service"]["water_ml_per_service"]
    service = ResourceServiceManager(world)
    for agent_id, queue_id in (
        ("A001", "water_station_main-left-fifo"),
        ("A002", "water_station_main-left-fifo"),
        ("A003", "water_station_main-right-fifo"),
    ):
        service.request(agent_id, "water_station_main", queue_id=queue_id)

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
    served = world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]["served"]
    assert served - served_before == 3 * per_service
    assert not world.facilities["water_station_main"].resource_queue_agent_ids
    assert not world.facilities["water_station_main"].resource_active_agent_ids
    world.validate()


@pytest.mark.xfail(
    strict=True,
    raises=StateInvariantError,
    reason="#118: a stall at the second queue's head fails the facility-wide head check",
)
def test_independent_water_queues_wait_for_room_when_the_station_serves_one(
    generated_scenario_with_split_queues,
) -> None:
    document = generated_scenario_with_split_queues.model_dump(mode="json")
    station = next(row for row in document["facilities"] if row["id"] == "water_station_main")
    station["capacity"] = station["working_units"] = 1
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    prepare_queued_agents(world, "water_station_main")
    service = ResourceServiceManager(world)
    for agent_id, queue_id in (
        ("A001", "water_station_main-left-fifo"),
        ("A002", "water_station_main-left-fifo"),
        ("A003", "water_station_main-right-fifo"),
    ):
        service.request(agent_id, "water_station_main", queue_id=queue_id)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))

    TickEngine(world, phase_handler=controller, rng=CounterRNG(world.seed)).run(16)

    served = [
        record.event.payload["agent_id"]
        for record in world.event_log.records
        if record.event.event_type == "resource_service_complete"
        and record.event.payload["facility_id"] == "water_station_main"
    ]
    assert sorted(served) == ["A001", "A002", "A003"]
