"""FIFO reservation order, need triage, and recorded queue exit/retry transitions."""

from __future__ import annotations

from typing import Any

from sheltersim.core.kernels import kernel_first
from sheltersim.core.world import WorldState
from sheltersim.domain.models import ActiveTask, Activity
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.policies.queueing import create_queue_policy


@kernel_first("queues", "prepare_facility_queue")
def prepare_facility_queue(
    world: WorldState, facility_id: str, *, resource: bool, minute: int
) -> None:
    """Apply the registered queue policy to the existing reservation list."""
    facility = world.facilities.get(facility_id)
    if facility is None:
        return
    queue = facility.resource_queue_agent_ids if resource else facility.queue_agent_ids
    if not queue:
        return
    policy = create_queue_policy(world.model_configs)
    movement = world.model_state.get("movement_v1", {})
    if isinstance(movement, dict) and any(
        journey.get("facility_id") == facility_id
        and journey.get("purpose")
        in {
            "toilet_queue_position",
            "resource_queue_position",
            "toilet_queue_service",
            "resource_queue_service",
        }
        for journey in movement.get("journeys", {}).values()
    ):
        return
    tasks = {
        task.agent_id: task
        for task in world.active_tasks.values()
        if task.resource_id == facility_id
        and task.kind == ("resource_service_queue" if resource else "toilet_queue")
    }
    service = _service_name(facility.kind)
    original_order = {agent_id: index for index, agent_id in enumerate(queue)}
    ranked: list[tuple[float, int, int, str]] = []
    for agent_id in tuple(queue):
        task = tasks.get(agent_id)
        if task is None:
            continue
        waited = max(0, minute - task.start_minute)
        urgency = policy.priority(world, agent_id, service, waited)
        ranked.append((-urgency, task.start_minute, original_order[agent_id], agent_id))
    queue[:] = [agent_id for _, _, _, agent_id in sorted(ranked)]
    if resource:
        _drop_stale_resource_stall(world, facility_id)
    for agent_id in tuple(queue):
        task = tasks.get(agent_id)
        if task is None:
            continue
        waited = max(0, minute - task.start_minute)
        if policy.should_abandon(world, agent_id, service, waited):
            _abandon_and_retry(
                world,
                facility_id=facility_id,
                agent_id=agent_id,
                task=task,
                resource=resource,
                service=service,
                minute=minute,
                retry_delay=policy.retry_delay_minutes(),
                policy_id=policy.model_id,
                source_id=policy.source_id,
            )


@kernel_first("queues", "prepare_independent_facility_queue")
def prepare_independent_facility_queue(
    world: WorldState, queue_id: str, *, minute: int, resource: bool
) -> None:
    """Apply only abandonment rules to an explicitly independent FIFO.

    The registered priority policy may end an overlong wait, but it does not reorder
    requests inside a queue whose contract promises FIFO service.
    """
    from sheltersim.domain.facility_queues import (
        FACILITY_QUEUES_STATE_KEY,
    )

    state = world.model_state.get(FACILITY_QUEUES_STATE_KEY)
    if not isinstance(state, dict) or queue_id not in state["queues"]:
        return
    movement = world.model_state.get("movement_v1", {})
    if isinstance(movement, dict) and any(
        journey.get("queue_id") == queue_id
        and journey.get("purpose")
        in {
            "toilet_queue_position",
            "resource_queue_position",
            "toilet_queue_service",
            "resource_queue_service",
        }
        for journey in movement.get("journeys", {}).values()
    ):
        return
    row = state["queues"][queue_id]
    if not row["queued_request_ids"]:
        return
    policy = create_queue_policy(world.model_configs)
    request_ids = tuple(row["queued_request_ids"])
    facility_id = row["facility_id"]
    facility = world.facilities[facility_id]
    service = _service_name(facility.kind)
    for request_id in request_ids:
        request = state["requests"].get(request_id)
        if request is None:
            continue
        task = world.active_tasks.get(request["queue_task_id"])
        if task is None:
            continue
        waited = max(0, minute - task.start_minute)
        if policy.should_abandon(world, request["agent_id"], service, waited):
            _abandon_and_retry(
                world,
                facility_id=facility_id,
                agent_id=request["agent_id"],
                task=task,
                resource=resource,
                service=service,
                minute=minute,
                retry_delay=policy.retry_delay_minutes(),
                policy_id=policy.model_id,
                source_id=policy.source_id,
                queue_id=queue_id,
            )


def _drop_stale_resource_stall(world: WorldState, facility_id: str) -> None:
    """Forget a stall once its Agent is no longer at the head of the queue.

    The stall record describes why the current head is waiting. Reordering or a head
    that leaves the queue makes it stale; the next arbitration records the new head.
    With independent queues, the head of any of the facility's queues stays current.
    """
    from sheltersim.domain.facility_queues import queue_head_agent_ids
    from sheltersim.domain.resource_service import SERVICE_STATE_KEY

    state = world.model_state.get(SERVICE_STATE_KEY)
    if not isinstance(state, dict):
        return
    record = state["stall_state"].get(facility_id)
    if record is not None and record["agent_id"] not in queue_head_agent_ids(world, facility_id):
        del state["stall_state"][facility_id]


def _abandon_and_retry(
    world: WorldState,
    *,
    facility_id: str,
    agent_id: str,
    task: ActiveTask,
    resource: bool,
    service: str,
    minute: int,
    retry_delay: int,
    policy_id: str,
    source_id: str,
    queue_id: str | None = None,
) -> None:
    facility = world.facilities[facility_id]
    queue = facility.resource_queue_agent_ids if resource else facility.queue_agent_ids
    if agent_id not in queue or agent_id not in world.agents:
        return
    movement_state = world.model_state.get("movement_v1")
    route = None
    route_points: tuple[Any, ...] = ()
    if movement_state is not None and "facility_geometry" in world.model_configs:
        from sheltersim.contracts.extensions import Point2D
        from sheltersim.domain.movement import (
            _geometry,
            _zone_center,
            in_room_travel_minutes,
            plan_in_room_route,
            start_movement,
        )
        from sheltersim.policies.space import ZoneRoute

        agent = world.agents[agent_id]
        geometry = _geometry(world)
        current_raw = movement_state["positions"].get(agent_id, {}).get("point")
        current = Point2D.model_validate(current_raw) if isinstance(current_raw, dict) else None
        destination = _zone_center(geometry, agent.zone_id) if geometry is not None else None
        if current is not None and destination is not None:
            path = plan_in_room_route(
                world,
                zone_id=agent.zone_id,
                start=current,
                destination=destination,
                mobility=agent.profile.mobility,
            )
            if path is None:
                return
            route_points = path
            route = ZoneRoute(
                origin_zone_id=agent.zone_id,
                destination_zone_id=agent.zone_id,
                zone_ids=(agent.zone_id, agent.zone_id),
                edge_ids=(),
                travel_minutes=in_room_travel_minutes(world, agent_id, path),
            )
            if len(path) < 2:
                route = route.__class__(
                    origin_zone_id=route.origin_zone_id,
                    destination_zone_id=route.destination_zone_id,
                    zone_ids=route.zone_ids,
                    edge_ids=route.edge_ids,
                    travel_minutes=0,
                )
    due_minute = minute + (route.travel_minutes if route is not None else 0)
    if due_minute >= world.duration_minutes:
        return
    sequence = world.allocate_event_sequence()
    abandon = ScheduledEvent(
        event_id=f"facility-queue-abandon-{sequence}",
        kind=EventKind.ENDOGENOUS,
        due_minute=minute,
        priority=0,
        sequence=sequence,
        event_type="facility_queue_abandon",
        payload={
            "facility_id": facility_id,
            "agent_id": agent_id,
            "task_id": task.id,
            "service": service,
            "start_minute": task.start_minute,
            "wait_minutes": minute - task.start_minute,
            "retry_at_minute": minute + retry_delay,
            "reason": "need_weighted_wait_threshold",
        },
        source_id=source_id,
        causation_id=task.id,
        provenance={
            "source_type": "assumption",
            "source_id": source_id,
            "policy_version": policy_id,
            "transition": "abandon_queue_then_retry",
        },
    )
    agent = world.agents[agent_id]
    queue.remove(agent_id)
    if resource:
        _drop_stale_resource_stall(world, facility_id)
    if queue_id is not None:
        from sheltersim.domain.facility_queues import (
            finish_service as finish_facility_queue_service,
        )
        from sheltersim.domain.facility_queues import (
            record_queue_transition,
        )

        request = finish_facility_queue_service(world, task.id, status="abandoned")
        if request is not None:
            record_queue_transition(
                world,
                request,
                transition="abandoned",
                task_id=task.id,
                wait_minutes=minute - request["requested_minute"],
                causation_id=task.id,
            )
    del world.active_tasks[task.id]
    agent.next_decision_at = minute + retry_delay
    world.event_log.append(
        EventLogRecord(
            event=abandon,
            realized_minute=minute,
            status=EventStatus.APPLIED,
            detail=(
                "Agent left a prolonged low-urgency queue and will reconsider after a bounded delay"
            ),
        )
    )
    if route is not None and route.travel_minutes > 0:
        from sheltersim.domain.movement import start_movement

        sequence = world.allocate_event_sequence()
        task_id = f"queue-abandon-travel-{sequence}"
        event_id = f"queue-abandon-arrival-{sequence}"
        due_minute = minute + route.travel_minutes
        travel_task = ActiveTask(
            id=task_id,
            kind="travel_return",
            agent_id=agent_id,
            resource_id=None,
            start_minute=minute,
            due_minute=due_minute,
        )
        travel_event = ScheduledEvent(
            event_id=event_id,
            kind=EventKind.COMPLETION,
            due_minute=due_minute,
            priority=0,
            sequence=sequence,
            event_type="agent_travel_complete",
            payload={
                "agent_id": agent_id,
                "task_id": task_id,
                "purpose": "queue_abandon",
                "destination_zone_id": route.destination_zone_id,
                "origin_zone_id": route.origin_zone_id,
                "route_zone_ids": list(route.zone_ids),
                "route_edge_ids": list(route.edge_ids),
                "travel_minutes": route.travel_minutes,
                "start_minute": minute,
            },
            source_id=task_id,
            causation_id=abandon.event_id,
            provenance={
                "source_type": "assumption",
                "source_id": source_id,
                "policy_version": policy_id,
                "movement": "in_room_return_to_zone_anchor",
            },
        )
        world.active_tasks[task_id] = travel_task
        agent.activity = Activity.MOVING
        agent.target_id = route.destination_zone_id
        world.schedule_event(travel_event)
        start_movement(
            world,
            agent_id=agent_id,
            route=route,
            purpose="queue_abandon",
            facility_id=None,
            started_minute=minute,
            due_minute=due_minute,
            event_id=event_id,
            task_id=task_id,
            route_points=route_points,
        )
    else:
        agent.activity = Activity.IDLE
        agent.target_id = None
    world.metric_state["facility_queue_abandon_count"] = (
        world.metric_state.get("facility_queue_abandon_count", 0.0) + 1.0
    )


def _service_name(facility_kind: str) -> str:
    if "toilet" in facility_kind.casefold():
        return "toilet"
    if facility_kind == "water_station":
        return "water"
    if facility_kind == "meal_distribution":
        return "meal"
    raise ValueError(f"unsupported queue service facility kind {facility_kind!r}")
