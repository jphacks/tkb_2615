"""Deterministic toilet requests, FIFO queues, and capacity arbitration."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sheltersim.core.kernels import kernel_first
from sheltersim.core.rng import KeyedRandom
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.facility_queues import (
    FACILITY_QUEUES_STATE_KEY,
    choose_queue_id,
    facility_has_independent_queues,
    mark_stall,
    point_for_queue,
    queue_ids_for_facility,
    record_queue_transition,
)
from sheltersim.domain.facility_queues import (
    enqueue as enqueue_facility_queue,
)
from sheltersim.domain.facility_queues import (
    finish_service as finish_facility_queue_service,
)
from sheltersim.domain.facility_queues import (
    start_service as start_facility_queue_service,
)
from sheltersim.domain.models import ActiveTask, Activity
from sheltersim.domain.movement import (
    physical_service_capacity,
    progress_physical_queue_head_to_service,
    progress_physical_queue_positions,
)
from sheltersim.domain.operational import staff_is_available
from sheltersim.domain.queueing import prepare_facility_queue, prepare_independent_facility_queue
from sheltersim.domain.staff_dynamics import staff_dynamics_is_available
from sheltersim.events.models import EventKind, ScheduledEvent

TOILET_SERVICE_POLICY_VERSION = "toilet-service-uniform-v1"

if TYPE_CHECKING:
    from sheltersim.core.engine import TickPhase


@dataclass(frozen=True)
class ToiletRequestReceipt:
    """Stable request identity and queue position at admission time."""

    agent_id: str
    facility_id: str
    requested_at: int
    queue_task_id: str
    queue_position: int


class ToiletServiceManager:
    """Accept toilet requests and start queued services in FIFO order."""

    policy_version = TOILET_SERVICE_POLICY_VERSION

    def __init__(
        self,
        world: WorldState,
        *,
        min_service_minutes: int = 4,
        max_service_minutes: int = 8,
    ):
        if (
            not isinstance(min_service_minutes, int)
            or isinstance(min_service_minutes, bool)
            or not isinstance(max_service_minutes, int)
            or isinstance(max_service_minutes, bool)
        ):
            raise ValueError("service duration bounds must be integers")
        if min_service_minutes <= 0 or max_service_minutes < min_service_minutes:
            raise ValueError("service duration bounds must be positive and ordered")
        self.world = world
        self.min_service_minutes = min_service_minutes
        self.max_service_minutes = max_service_minutes

    @kernel_first("toilet", "request")
    def request(
        self,
        agent_id: str,
        facility_id: str,
        *,
        minute: int | None = None,
        validate_state: bool = True,
        queue_id: str | None = None,
    ) -> ToiletRequestReceipt:
        """Place one eligible Agent at the tail of a toilet Facility's FIFO queue."""
        world = self.world
        request_minute = world.minute_index if minute is None else minute
        if not isinstance(request_minute, int) or isinstance(request_minute, bool):
            raise ValueError("toilet request minute must be an integer")
        if request_minute != world.minute_index:
            raise ValueError("toilet request minute must match WorldState")
        if validate_state:
            world.validate()
        agent = world.agents.get(agent_id)
        if agent is None:
            raise ValueError(f"unknown Agent {agent_id!r}")
        facility = world.facilities.get(facility_id)
        if facility is None or "toilet" not in facility.kind.casefold():
            raise ValueError(f"{facility_id!r} is not a toilet Facility")
        if agent.activity not in (Activity.IDLE, Activity.RESTING, Activity.SLEEPING):
            raise ValueError(f"Agent {agent_id!r} cannot request a toilet while active")
        if any(
            agent_id in item.queue_agent_ids or agent_id in item.active_agent_ids
            for item in world.facilities.values()
        ):
            raise ValueError(f"Agent {agent_id!r} already has an active toilet request")

        digest = hashlib.sha256(
            f"{facility_id}\0{agent_id}\0{request_minute}".encode("utf-8")
        ).hexdigest()[:24]
        task_id = f"toilet-queue-{digest}"
        if task_id in world.active_tasks:
            raise StateInvariantError(f"duplicate toilet queue task ID {task_id!r}")
        task = ActiveTask(
            id=task_id,
            kind="toilet_queue",
            agent_id=agent_id,
            resource_id=facility_id,
            start_minute=request_minute,
            due_minute=request_minute,
        )

        selected_queue = queue_id
        if facility_has_independent_queues(world, facility_id):
            selected_queue = selected_queue or choose_queue_id(world, facility_id, agent_id)
            point = point_for_queue(world, selected_queue) if selected_queue else None
            if point is None or point.facility_id != facility_id:
                raise ValueError("no accessible independent toilet service queue is available")
            facility.queue_agent_ids.append(agent_id)
            enqueue_facility_queue(
                world,
                queue_id=selected_queue,
                request_id=task_id,
                task_id=task_id,
                agent_id=agent_id,
                service_kind="toilet",
                requested_minute=request_minute,
            )
            queue_position = len(
                world.model_state[FACILITY_QUEUES_STATE_KEY]["queues"][selected_queue][
                    "queued_request_ids"
                ]
            )
        else:
            facility.queue_agent_ids.append(agent_id)
            queue_position = len(facility.queue_agent_ids)
        world.active_tasks[task_id] = task
        agent.activity = Activity.QUEUED
        agent.target_id = facility_id
        if selected_queue is not None:
            record_queue_transition(
                world,
                world.model_state[FACILITY_QUEUES_STATE_KEY]["requests"][task_id],
                transition="queued",
                task_id=task_id,
            )
        if validate_state:
            world.validate()
        return ToiletRequestReceipt(
            agent_id=agent_id,
            facility_id=facility_id,
            requested_at=request_minute,
            queue_task_id=task_id,
            queue_position=queue_position,
        )

    def __call__(self, phase: TickPhase, world: WorldState, minute: int, rng: KeyedRandom) -> None:
        """Use as a tick phase callback; capacity is arbitrated after proposals."""
        if world is not self.world:
            raise ValueError("toilet service manager is bound to a different WorldState")
        if phase == "arbitrate_actions":
            self.arbitrate(minute, rng)

    def arbitrate(
        self,
        minute: int,
        rng: KeyedRandom,
        *,
        validate_state: bool = True,
    ) -> tuple[ScheduledEvent, ...]:
        """Start as many FIFO requests as operational capacity permits."""
        world = self.world
        if minute != world.minute_index:
            raise ValueError("facility arbitration minute must match WorldState")
        started: list[ScheduledEvent] = []
        for facility in self._facilities_to_serve():
            if facility_has_independent_queues(world, facility.id):
                started.extend(self._arbitrate_independent_facility(facility, minute, rng))
                continue
            prepare_facility_queue(world, facility.id, resource=False, minute=minute)
            while facility.queue_agent_ids and len(
                facility.active_agent_ids
            ) < physical_service_capacity(world, facility.id, facility.working_units):
                if progress_physical_queue_positions(
                    world, facility.id, resource=False, minute=minute
                ):
                    break
                if progress_physical_queue_head_to_service(
                    world, facility.id, resource=False, minute=minute
                ):
                    break
                agent_id = facility.queue_agent_ids[0]
                queue_task = self._queue_task(facility.id, agent_id)
                queue_wait = minute - queue_task.start_minute
                if queue_wait < 0:
                    raise StateInvariantError("toilet queue time cannot be in the future")

                rng_key = (
                    "toilet-service",
                    facility.id,
                    agent_id,
                    "minute",
                    minute,
                    self.policy_version,
                    "duration",
                )
                draw = rng.random(*rng_key)
                if not 0.0 <= draw < 1.0:
                    raise ValueError("keyed RNG draw must be between zero and one")
                duration_span = self.max_service_minutes - self.min_service_minutes + 1
                service_minutes = self.min_service_minutes + int(draw * duration_span)
                service_minutes = min(service_minutes, self.max_service_minutes)
                due_minute = minute + service_minutes
                if due_minute >= world.duration_minutes:
                    # Keep FIFO order when the head request cannot finish before the run ends.
                    break

                sequence, event_id, task_id = self._allocate_service_ids()
                task = ActiveTask(
                    id=task_id,
                    kind="toilet_service",
                    agent_id=agent_id,
                    resource_id=facility.id,
                    start_minute=minute,
                    due_minute=due_minute,
                )
                event = ScheduledEvent(
                    event_id=event_id,
                    kind=EventKind.COMPLETION,
                    due_minute=due_minute,
                    priority=0,
                    sequence=sequence,
                    event_type="toilet_service_complete",
                    payload={
                        "facility_id": facility.id,
                        "agent_id": agent_id,
                        "task_id": task_id,
                    },
                    source_id=task_id,
                    causation_id=queue_task.id,
                    provenance={
                        "source_type": "assumption",
                        "source_id": "ASM-TOILET-SERVICE-DURATION-V1",
                        "policy_version": self.policy_version,
                        "duration_distribution": "discrete_uniform_inclusive",
                        "minimum_minutes": self.min_service_minutes,
                        "maximum_minutes": self.max_service_minutes,
                        "service_minutes": service_minutes,
                        "queue_wait_minutes": queue_wait,
                        "requested_at": queue_task.start_minute,
                        "service_started_at": minute,
                        "rng_key": list(rng_key),
                        "rng_draw": draw,
                    },
                )

                facility.queue_agent_ids.pop(0)
                facility.active_agent_ids.append(agent_id)
                del world.active_tasks[queue_task.id]
                world.active_tasks[task.id] = task
                agent = world.agents[agent_id]
                agent.activity = Activity.IN_SERVICE
                agent.target_id = facility.id
                world.schedule_event(event)
                self._record_queue_wait(queue_wait)
                started.append(event)

        if validate_state:
            world.validate()
        return tuple(started)

    @kernel_first("toilet", "facilities_to_serve")
    def _facilities_to_serve(self) -> list[Any]:
        """The toilet facilities in ID order."""
        return [
            facility
            for facility in sorted(self.world.facilities.values(), key=lambda item: item.id)
            if "toilet" in facility.kind.casefold()
        ]

    def _arbitrate_independent_facility(
        self, facility: object, minute: int, rng: KeyedRandom
    ) -> list[ScheduledEvent]:
        world = self.world
        queue_state = world.model_state[FACILITY_QUEUES_STATE_KEY]
        started: list[ScheduledEvent] = []
        facility_limit = min(facility.capacity, facility.working_units)
        for queue_id in queue_ids_for_facility(world, facility.id):
            prepare_independent_facility_queue(world, queue_id, minute=minute, resource=False)
            queue = queue_state["queues"][queue_id]
            point = point_for_queue(world, queue_id)
            if point is None:
                raise StateInvariantError(f"service queue {queue_id!r} lost its signed point")
            while queue["queued_request_ids"]:
                if progress_physical_queue_positions(
                    world, facility.id, resource=False, minute=minute, queue_id=queue_id
                ):
                    break
                if (
                    len(queue["active_request_ids"])
                    >= physical_service_capacity(
                        world, facility.id, point.service_capacity, queue_id
                    )
                    or len(facility.active_agent_ids) >= facility_limit
                ):
                    request_id = queue["queued_request_ids"][0]
                    mark_stall(world, queue_id, request_id, "capacity")
                    break
                if progress_physical_queue_head_to_service(
                    world, facility.id, resource=False, minute=minute, queue_id=queue_id
                ):
                    break
                request_id = queue["queued_request_ids"][0]
                request = queue_state["requests"][request_id]
                agent_id = request["agent_id"]
                queue_task = world.active_tasks.get(request["queue_task_id"])
                if queue_task is None or queue_task.kind != "toilet_queue":
                    raise StateInvariantError("independent toilet queue lost its active task")
                queue_wait = minute - queue_task.start_minute
                if queue_wait < 0:
                    raise StateInvariantError("toilet queue time cannot be in the future")
                rng_key = (
                    "toilet-service",
                    facility.id,
                    agent_id,
                    "minute",
                    minute,
                    self.policy_version,
                    "duration",
                    queue_id,
                )
                draw = rng.random(*rng_key)
                if not 0.0 <= draw < 1.0:
                    raise ValueError("keyed RNG draw must be between zero and one")
                duration_span = self.max_service_minutes - self.min_service_minutes + 1
                service_minutes = min(
                    self.max_service_minutes,
                    self.min_service_minutes + int(draw * duration_span),
                )
                due_minute = minute + service_minutes
                if due_minute >= world.duration_minutes:
                    mark_stall(world, queue_id, request_id, "run_end")
                    break
                required_staff_skills = set(point.access.required_staff_skills)
                staff_member = (
                    next(
                        (
                            person
                            for person in sorted(world.staff.values(), key=lambda item: item.id)
                            if required_staff_skills <= set(person.skills)
                            and person.zone_id == facility.zone_id
                            and person.active_task_id is None
                            and staff_is_available(world, person.id)
                            and staff_dynamics_is_available(world, person.id, minute)
                        ),
                        None,
                    )
                    if required_staff_skills
                    else None
                )
                if required_staff_skills and staff_member is None:
                    mark_stall(world, queue_id, request_id, "staff")
                    break
                sequence, event_id, task_id = self._allocate_service_ids()
                task = ActiveTask(
                    id=task_id,
                    kind="toilet_service",
                    agent_id=agent_id,
                    resource_id=facility.id,
                    start_minute=minute,
                    due_minute=due_minute,
                    staff_id=staff_member.id if staff_member is not None else None,
                )
                event = ScheduledEvent(
                    event_id=event_id,
                    kind=EventKind.COMPLETION,
                    due_minute=due_minute,
                    priority=0,
                    sequence=sequence,
                    event_type="toilet_service_complete",
                    payload={
                        "facility_id": facility.id,
                        "agent_id": agent_id,
                        "task_id": task_id,
                        "staff_id": staff_member.id if staff_member is not None else None,
                    },
                    source_id=task_id,
                    causation_id=queue_task.id,
                    provenance={
                        "source_type": "assumption",
                        "source_id": "ASM-TOILET-SERVICE-DURATION-V1",
                        "policy_version": self.policy_version,
                        "duration_distribution": "discrete_uniform_inclusive",
                        "minimum_minutes": self.min_service_minutes,
                        "maximum_minutes": self.max_service_minutes,
                        "service_minutes": service_minutes,
                        "queue_wait_minutes": queue_wait,
                        "requested_at": queue_task.start_minute,
                        "service_started_at": minute,
                        "rng_key": list(rng_key),
                        "rng_draw": draw,
                        "queue_id": queue_id,
                    },
                )
                if agent_id not in facility.queue_agent_ids:
                    raise StateInvariantError(
                        "independent toilet request is missing from facility projection"
                    )
                facility.queue_agent_ids.remove(agent_id)
                facility.active_agent_ids.append(agent_id)
                del world.active_tasks[queue_task.id]
                world.active_tasks[task.id] = task
                if staff_member is not None:
                    staff_member.active_task_id = task.id
                    staff_member.work_queue[:] = [task.id]
                    staff_member.busy_until = task.due_minute
                world.agents[agent_id].activity = Activity.IN_SERVICE
                world.agents[agent_id].target_id = facility.id
                world.schedule_event(event)
                active_request = start_facility_queue_service(world, request_id, task.id)
                record_queue_transition(
                    world,
                    active_request,
                    transition="service_started",
                    task_id=task.id,
                    wait_minutes=queue_wait,
                    causation_id=event.event_id,
                )
                self._record_queue_wait(queue_wait)
                started.append(event)
        return started

    def _queue_task(self, facility_id: str, agent_id: str) -> ActiveTask:
        matches = [
            task
            for task in self.world.active_tasks.values()
            if task.kind == "toilet_queue"
            and task.agent_id == agent_id
            and task.resource_id == facility_id
        ]
        if len(matches) != 1:
            raise StateInvariantError(
                f"queued Agent {agent_id!r} must have exactly one matching queue task"
            )
        return matches[0]

    def _allocate_service_ids(self) -> tuple[int, str, str]:
        world = self.world
        pending_event_ids = {event.event_id for event in world.event_queue.pending()}
        while True:
            sequence = world.allocate_event_sequence()
            event_id = f"toilet-complete-{sequence}"
            task_id = f"toilet-service-{sequence}"
            if (
                event_id not in pending_event_ids
                and not world.event_log.contains_id(event_id)
                and task_id not in world.active_tasks
            ):
                return sequence, event_id, task_id

    def _record_queue_wait(self, wait_minutes: int) -> None:
        metrics = self.world.metric_state
        metrics["toilet_wait_count"] = metrics.get("toilet_wait_count", 0.0) + 1.0
        metrics["toilet_wait_total_minutes"] = (
            metrics.get("toilet_wait_total_minutes", 0.0) + wait_minutes
        )
        metrics["toilet_wait_max_minutes"] = max(
            metrics.get("toilet_wait_max_minutes", 0.0), float(wait_minutes)
        )


def complete_toilet_service(
    world: WorldState,
    *,
    facility_id: str,
    agent_id: str,
    task_id: str,
) -> None:
    """Release one matching service slot after its typed completion event."""
    facility = world.facilities.get(facility_id)
    agent = world.agents.get(agent_id)
    task = world.active_tasks.get(task_id)
    if facility is None or "toilet" not in facility.kind.casefold():
        raise ValueError(f"{facility_id!r} is not a toilet Facility")
    if agent is None:
        raise ValueError(f"Agent {agent_id!r} does not exist")
    if task is None or task.kind != "toilet_service":
        raise ValueError(f"toilet service task {task_id!r} is not active")
    if task.agent_id != agent_id or task.resource_id != facility_id:
        raise ValueError("toilet completion payload does not match its active task")
    if agent.activity is not Activity.IN_SERVICE or agent.target_id != facility_id:
        raise ValueError(f"Agent {agent_id!r} is not using Facility {facility_id!r}")
    if facility.active_agent_ids.count(agent_id) != 1:
        raise ValueError(f"Agent {agent_id!r} must occupy exactly one toilet service slot")

    queue_request = finish_facility_queue_service(world, task_id, status="completed")
    if queue_request is not None:
        record_queue_transition(
            world,
            queue_request,
            transition="completed",
            task_id=task_id,
            wait_minutes=task.start_minute - queue_request["requested_minute"],
            causation_id=task_id,
        )
    facility.active_agent_ids.remove(agent_id)
    if task.staff_id is not None:
        staff_member = world.staff.get(task.staff_id)
        if (
            staff_member is None
            or staff_member.active_task_id != task.id
            or staff_member.work_queue != [task.id]
            or staff_member.busy_until != task.due_minute
        ):
            raise StateInvariantError("toilet service staff reservation is inconsistent")
        staff_member.active_task_id = None
        staff_member.work_queue.clear()
        staff_member.busy_until = 0
    del world.active_tasks[task_id]
    agent.activity = Activity.IDLE
    agent.target_id = None
    agent.needs.toilet_urgency = 0.0
    world.metric_state["toilet_service_completed_count"] = (
        world.metric_state.get("toilet_service_completed_count", 0.0) + 1.0
    )
