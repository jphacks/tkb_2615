"""A resident walks to the support worker, run through the tick order.

What happens when the route closes or the worker leaves on the way is checked
by hand-driven unit tests in tests/unit/domain/test_staff.py.
"""

from __future__ import annotations

import json

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.domain.care_receipts import (
    append_applied_care_transition,
    care_source_ids_for_agent,
)
from sheltersim.domain.models import SupportRequestStatus
from sheltersim.domain.population_lifecycle import current_household_id
from sheltersim.experiments.metrics import MetricSnapshot, MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import _MetricSink
from sheltersim.inspection.frames import person_projection

WORKER = "staff_001"


def _controller(scenario: ScenarioSpec) -> OfflineSimulationController:
    return OfflineSimulationController(
        WorldState.from_scenario(scenario), _MetricSink(MetricsRecorder())
    )


def _request_and_assign(controller: OfflineSimulationController) -> str:
    world = controller.world
    receipt = controller.staff_service.request("A001")
    append_applied_care_transition(
        world,
        request_id=receipt.request_id,
        agent_id="A001",
        household_id=current_household_id(world, "A001"),
        support_kind="emotional_support",
        status="requested",
        source_ids=care_source_ids_for_agent(world, "A001"),
    )
    assert controller.staff_service.assign_pending(0, validate_state=False) == (receipt.request_id,)
    request = world.support_requests[receipt.request_id]
    append_applied_care_transition(
        world,
        request_id=request.id,
        agent_id=request.agent_id,
        household_id=current_household_id(world, "A001"),
        support_kind="emotional_support",
        status="assigned",
        staff_id=request.assigned_staff_id,
        causation_id=request.task_id,
        source_ids=care_source_ids_for_agent(world, "A001"),
    )
    world.validate()
    return receipt.request_id


def test_resident_travels_to_staff_before_support_starts_and_checkpoint_round_trips(
    generated_scenario,
) -> None:
    controller = _controller(generated_scenario)
    world = controller.world
    task_minutes = world.model_configs["staff"]["support_task_minutes"]
    request_id = _request_and_assign(controller)

    resident_trip = next(
        task for task in world.active_tasks.values() if task.kind == "staff_support_travel"
    )
    support_task = world.active_tasks[world.support_requests[request_id].task_id]
    assert world.agents["A001"].activity.value == "moving"
    assert support_task.start_minute == resident_trip.due_minute
    assert support_task.due_minute == resident_trip.due_minute + task_minutes
    assert world.model_state["movement_v1"]["journeys"]["A001"]["support_request_id"] == request_id
    snapshot = MetricSnapshot.from_world(world)
    assert snapshot.unresolved_support_request_count == 1
    metrics_sources = json.loads(snapshot.source_ids_json)
    assert metrics_sources["unresolved_support_agents"] == ["A001"]
    assert metrics_sources["support_wait_by_agent"]["A001"] == {
        "request_id": request_id,
        "waited_minutes": 0,
        "reason_codes": ["resident_travel"],
    }
    care_request = person_projection(world, "A001")["person"]["care_requests"][0]
    assert care_request["waited_minutes"] == 0
    assert care_request["wait_reason_codes"] == ["resident_travel"]
    world.validate()
    restored = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    assert restored.canonical_json() == world.canonical_json()

    engine = TickEngine(world, phase_handler=controller)
    while world.minute_index <= resident_trip.due_minute:
        engine.step()
    assert world.agents["A001"].zone_id == world.staff[WORKER].zone_id
    assert world.agents["A001"].activity.value == "seeking_help"
    assert world.support_requests[request_id].status is SupportRequestStatus.ASSIGNED
    assert not any(task.kind == "staff_support_travel" for task in world.active_tasks.values())
    world.validate()
