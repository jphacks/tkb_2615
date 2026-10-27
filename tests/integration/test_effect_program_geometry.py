"""A typed geometry EffectProgram applied mid-trip, then the run continues with TickEngine.

On the shared generated Scenario, with the toilet's two independent queues
(generated_scenario_with_split_queues). Moved from tests/test_effect_program.py,
where it ran on a hand-made floor plan without facility awareness (#88).
"""

from __future__ import annotations

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickEngine, TickPhase, TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.facilities import ToiletServiceManager
from sheltersim.events.effects import (
    EffectProgram,
    SupportMode,
    TargetSelector,
    TimedAction,
    schedule_effect_program,
)
from sheltersim.events.models import EventStatus
from sheltersim.experiments.runner import OfflineSimulationController

# The far one of toilet_01's two queues: the near one's queue spot is so close to
# the toilet area's entrance that a partition on the way would block the declared
# corridor passage, which preflight rejects.
QUEUE_ID = "toilet_01-right-fifo"
# Slow enough that the resident is still walking to the queue when the partition goes up.
SLOW_WALK_M_PER_MINUTE = 0.2


class ArtifactSink:
    trace_level = TraceLevel.OFF

    def record_decision(self, *_args: object, **_kwargs: object) -> None:
        pass

    def capture_metric(self, *_args: object, **_kwargs: object) -> None:
        pass


def _partition_program(polygon: list[dict[str, float]]) -> EffectProgram:
    return EffectProgram(
        program_id="program-h18-test",
        narrative="Bounded test program",
        source_text="test source text",
        simulation_start_minute=2,
        effects=[
            TimedAction(
                effect_id="queue-walk-partition",
                op="set_partition",
                target_selector=TargetSelector(kind="zone", target_id="toilet"),
                start_minute=2,
                end_minute=20,
                parameters={"polygon_m": polygon},
                units="metre_2d",
                source_id="ASM-H18-EFFECT-TEST-V1",
                assumption_ids=[],
                policy_dependencies=["geometry_overlay_v1"],
                reversible=True,
            )
        ],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )


def _square_between(
    start: dict[str, float], end: dict[str, float], half_side: float = 0.25
) -> list[dict[str, float]]:
    """A small square on the straight line halfway from start to end."""
    x = (start["x"] + end["x"]) / 2
    y = (start["y"] + end["y"]) / 2
    return [
        {"x": x - half_side, "y": y - half_side},
        {"x": x + half_side, "y": y - half_side},
        {"x": x + half_side, "y": y + half_side},
        {"x": x - half_side, "y": y + half_side},
    ]


def test_queue_trip_replans_after_typed_partition_and_keeps_fifo_reservation(
    generated_scenario_with_split_queues,
) -> None:
    world = WorldState.from_scenario(generated_scenario_with_split_queues)
    speeds = world.model_configs["movement_model"]["speed_m_per_minute_by_mobility"]
    for mobility in speeds:
        speeds[mobility] = SLOW_WALK_M_PER_MINUTE
    # A001 stands where the corridor route enters the toilet area, so the walk to
    # the queue is in-room.
    routes = world.model_configs["facility_geometry"]["routes"]
    entrance = next(route for route in routes if route["edge_id"] == "route_corridor_toilet")
    world.agents["A001"].zone_id = "toilet"
    world.agents["A001"].next_decision_at = 100
    world.model_state["movement_v1"]["positions"]["A001"].update(
        {
            "zone_id": "toilet",
            "point": dict(entrance["points"][-1]),
            "edge_id": None,
            "progress": 0.0,
            "position_kind": "physical",
        }
    )
    service = ToiletServiceManager(world, min_service_minutes=2, max_service_minutes=2)
    request = service.request("A001", "toilet_01", queue_id=QUEUE_ID)
    service.arbitrate(0, CounterRNG(world.seed))
    initial = dict(world.model_state["movement_v1"]["journeys"]["A001"])
    assert initial["purpose"] == "toilet_queue_position"
    # A partition across the straight walk to the queue, clear of the service point.
    obstacle = _partition_program(
        _square_between(initial["route_points"][0], initial["route_points"][-1])
    )
    schedule_effect_program(world, obstacle)

    # Generated people carry minute-by-minute psychology, so each minute's agent
    # update runs in order, as in a tick.
    controller = OfflineSimulationController(world, ArtifactSink())
    controller(TickPhase.UPDATE_AGENT_STATE, world, 0, CounterRNG(world.seed))
    world.minute_index = 1
    controller(TickPhase.DISPATCH_DUE_EVENTS, world, 1, CounterRNG(world.seed))
    controller(TickPhase.UPDATE_AGENT_STATE, world, 1, CounterRNG(world.seed))
    live_position = dict(world.model_state["movement_v1"]["positions"]["A001"]["point"])
    world.minute_index = 2
    controller(TickPhase.DISPATCH_DUE_EVENTS, world, 2, CounterRNG(world.seed))
    controller(TickPhase.UPDATE_AGENT_STATE, world, 2, CounterRNG(world.seed))

    replanned = world.model_state["movement_v1"]["journeys"]["A001"]
    assert replanned["purpose"] == "toilet_queue_position"
    assert replanned["queue_id"] == QUEUE_ID
    assert replanned["route_points"] != initial["route_points"]
    assert replanned["route_points"][0] == live_position
    assert replanned["event_id"] != initial["event_id"]
    assert world.agents["A001"].activity.value == "queued"
    queue_request = world.model_state["facility_queues_v1"]["requests"][request.queue_task_id]
    assert queue_request["queue_id"] == QUEUE_ID
    assert (
        next(
            record
            for record in world.event_log.records
            if record.event.event_id == initial["event_id"]
        ).status
        is EventStatus.SUPPRESSED
    )
    assert world.metric_state["movement_replan_count"] == 1.0
    world.validate()

    # Continue from the next minute, as after a finished tick, long enough for the
    # slow walk and the generated toilet's longest service.
    world.minute_index = 3
    restored = Checkpoint.capture(world).restore_world()
    assert restored.canonical_json() == world.canonical_json()
    restored_controller = OfflineSimulationController(restored, ArtifactSink())
    TickEngine(
        restored,
        phase_handler=restored_controller,
        rng=CounterRNG(restored.seed),
    ).run(60)
    assert any(
        record.status is EventStatus.APPLIED
        and record.event.event_type == "facility_queue_transition"
        and record.event.payload.get("transition") == "completed"
        and record.event.payload.get("queue_id") == QUEUE_ID
        for record in restored.event_log.records
    )
    queue = restored.model_state["facility_queues_v1"]["queues"][QUEUE_ID]
    assert not queue["queued_request_ids"] and not queue["active_request_ids"]
    restored.validate()
