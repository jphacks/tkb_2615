"""Deterministic staffed water and meal service for ResourceLedger v1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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
    request_for_task as facility_queue_request_for_task,
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
from sheltersim.domain.operational import staff_is_available, water_service_blocked
from sheltersim.domain.queueing import prepare_facility_queue, prepare_independent_facility_queue
from sheltersim.domain.resources import RESOURCE_LEDGER_KEY, apply_resource_operation
from sheltersim.domain.staff_dynamics import staff_dynamics_is_available
from sheltersim.events.models import (
    EventApplicationError,
    EventKind,
    EventLogRecord,
    EventStatus,
    EventSuppressed,
    ScheduledEvent,
)

if TYPE_CHECKING:
    from sheltersim.core.rng import KeyedRandom


SERVICE_STATE_KEY = "resource_service_v1"


@dataclass(frozen=True)
class ResourceRequestReceipt:
    """Stable queue receipt for one water or meal service request."""

    agent_id: str
    facility_id: str
    resource_id: str
    requested_at: int
    queue_task_id: str
    queue_position: int


class ResourceServiceManager:
    """Manage FIFO requests, one-staff service starts, and ledger-backed completion."""

    policy_version = "resource-service-v1"

    def __init__(self, world: WorldState) -> None:
        if world.schema_version != 6 or "resource_service" not in world.model_configs:
            raise ValueError("ResourceServiceManager requires an opted-in WorldState v6")
        self.world = world
        self.config = dict(world.model_configs["resource_service"])
        if (
            SERVICE_STATE_KEY not in world.model_state
            or RESOURCE_LEDGER_KEY not in world.model_state
        ):
            raise ValueError("WorldState v6 requires resource service and ledger state")

    def request(
        self,
        agent_id: str,
        facility_id: str,
        *,
        minute: int | None = None,
        validate_state: bool = True,
        queue_id: str | None = None,
    ) -> ResourceRequestReceipt:
        """Put an eligible Agent at the tail of the correct resource FIFO queue."""
        world = self.world
        request_minute = world.minute_index if minute is None else minute
        if not isinstance(request_minute, int) or isinstance(request_minute, bool):
            raise ValueError("resource request minute must be an integer")
        if request_minute != world.minute_index:
            raise ValueError("resource request minute must match WorldState")
        if validate_state:
            world.validate()
        agent = world.agents.get(agent_id)
        facility = world.facilities.get(facility_id)
        if agent is None:
            raise ValueError(f"unknown Agent {agent_id!r}")
        if facility is None or self.resource_for_facility(facility.kind) is None:
            raise ValueError(f"{facility_id!r} is not a resource service facility")
        if agent.zone_id != facility.zone_id:
            raise ValueError("Agent must arrive at the resource service facility before queueing")
        if agent.activity not in (Activity.IDLE, Activity.RESTING, Activity.SLEEPING):
            raise ValueError(f"Agent {agent_id!r} cannot request resource service while active")
        if self._agent_has_exclusive_task(agent_id):
            raise ValueError(f"Agent {agent_id!r} already has an exclusive task")

        counter = world.id_counters.get("resource_task", 0)
        task_id, counter = self._next_queue_task_id(counter)
        world.id_counters["resource_task"] = counter
        selected_queue = queue_id
        if facility_has_independent_queues(world, facility_id):
            selected_queue = selected_queue or choose_queue_id(world, facility_id, agent_id)
            point = point_for_queue(world, selected_queue) if selected_queue else None
            if point is None or point.facility_id != facility_id:
                raise ValueError("no accessible independent resource service queue is available")
            facility.resource_queue_agent_ids.append(agent_id)
            enqueue_facility_queue(
                world, queue_id=selected_queue, request_id=task_id, task_id=task_id,
                agent_id=agent_id,
                service_kind="water" if self.resource_for_facility(facility.kind) == "water_ml" else "meal",
                requested_minute=request_minute,
            )
            queue_position = len(world.model_state[FACILITY_QUEUES_STATE_KEY]["queues"][selected_queue]["queued_request_ids"])
        else:
            facility.resource_queue_agent_ids.append(agent_id)
            queue_position = len(facility.resource_queue_agent_ids)
        world.active_tasks[task_id] = ActiveTask(
            id=task_id,
            kind="resource_service_queue",
            agent_id=agent_id,
            resource_id=facility_id,
            start_minute=request_minute,
            due_minute=request_minute,
        )
        agent.activity = Activity.QUEUED
        agent.target_id = facility_id
        if selected_queue is not None:
            record_queue_transition(
                world,
                world.model_state[FACILITY_QUEUES_STATE_KEY]["requests"][task_id],
                transition="queued", task_id=task_id,
            )
        action = (
            "fetch_water"
            if self.resource_for_facility(facility.kind) == "water_ml"
            else "collect_meal"
        )
        self.record_request_outcome(
            agent_id=agent_id,
            action=action,
            outcome="queued",
            reason="arrived_at_facility",
            facility_id=facility_id,
            origin_zone_id=agent.zone_id,
        )
        if validate_state:
            world.validate()
        return ResourceRequestReceipt(
            agent_id=agent_id,
            facility_id=facility_id,
            resource_id=self.resource_for_facility(facility.kind),
            requested_at=request_minute,
            queue_task_id=task_id,
            queue_position=queue_position,
        )

    def arbitrate(
        self,
        minute: int,
        rng: KeyedRandom | None = None,
        *,
        validate_state: bool = True,
    ) -> tuple[ScheduledEvent, ...]:
        """Start available FIFO heads in stable facility and staff ID order."""
        del rng  # Service duration is a versioned fixed assumption, not a random draw.
        world = self.world
        if minute != world.minute_index:
            raise ValueError("resource service arbitration minute must match WorldState")
        started: list[ScheduledEvent] = []
        for facility in sorted(world.facilities.values(), key=lambda item: item.id):
            resource_id = self.resource_for_facility(facility.kind)
            if resource_id is None:
                continue
            if facility_has_independent_queues(world, facility.id):
                started.extend(self._arbitrate_independent_facility(facility, resource_id, minute))
                continue
            prepare_facility_queue(world, facility.id, resource=True, minute=minute)
            while facility.resource_queue_agent_ids:
                if progress_physical_queue_positions(
                    world, facility.id, resource=True, minute=minute
                ):
                    break
                agent_id = facility.resource_queue_agent_ids[0]
                queue_task = self._queue_task(facility.id, agent_id)
                self._assert_service_task_id_available(queue_task.id)
                if resource_id == "water_ml" and water_service_blocked(world, facility.id):
                    self._record_queue_stall(facility.id, agent_id, "water_outage")
                    break
                operational_capacity = physical_service_capacity(
                    world, facility.id, min(facility.capacity, facility.working_units)
                )
                if len(facility.resource_active_agent_ids) >= operational_capacity:
                    self._record_queue_stall(
                        facility.id, facility.resource_queue_agent_ids[0], "capacity"
                    )
                    break
                skill = self.config[
                    "water_staff_skill" if resource_id == "water_ml" else "meal_staff_skill"
                ]
                staff_member = next(
                    (
                        person
                        for person in sorted(world.staff.values(), key=lambda item: item.id)
                        if person.zone_id == facility.zone_id
                        and skill in person.skills
                        and person.active_task_id is None
                        and staff_is_available(world, person.id)
                        and staff_dynamics_is_available(world, person.id, minute)
                    ),
                    None,
                )
                if staff_member is None:
                    self._record_queue_stall(facility.id, agent_id, "staff")
                    break
                account = world.model_state[RESOURCE_LEDGER_KEY]["accounts"][resource_id]
                quantity = self.config[
                    "water_ml_per_service"
                    if resource_id == "water_ml"
                    else "food_meal_units_per_service"
                ]
                if account["remaining"] - account["reserved"] < quantity:
                    self._record_queue_stall(facility.id, agent_id, "stock")
                    break
                service_minutes = self.config["service_minutes"]
                due_minute = minute + service_minutes
                if due_minute >= world.duration_minutes:
                    self._record_queue_stall(facility.id, agent_id, "run_end")
                    break
                if progress_physical_queue_head_to_service(
                    world, facility.id, resource=True, minute=minute
                ):
                    break
                completion_sequence = world.id_counters.get("event", 0)
                completion_event = ScheduledEvent(
                    event_id=f"resource-service-complete-{completion_sequence}",
                    kind=EventKind.COMPLETION,
                    due_minute=due_minute,
                    priority=0,
                    sequence=completion_sequence,
                    event_type="resource_service_complete",
                    payload={
                        "facility_id": facility.id,
                        "agent_id": agent_id,
                        "task_id": queue_task.id,
                        "resource_id": resource_id,
                        "reservation_id": queue_task.id,
                        "quantity": quantity,
                        "staff_id": staff_member.id,
                    },
                    source_id=queue_task.id,
                    causation_id=queue_task.id,
                    provenance=self._provenance(
                        action="service_scheduled",
                        service_minutes=service_minutes,
                    ),
                )
                self._ensure_event_identity_free(completion_event)
                if world.allocate_event_sequence() != completion_sequence:
                    raise StateInvariantError("event sequence changed during resource preflight")
                reservation_event = self._audit_event(
                    "reserve",
                    {
                        "warehouse_id": self._warehouse_id(),
                        "resource_id": resource_id,
                        "reservation_id": queue_task.id,
                        "agent_id": agent_id,
                        "quantity": quantity,
                    },
                    causation_id=queue_task.id,
                )
                try:
                    apply_resource_operation(world, reservation_event, "reserve")
                except ValueError as exc:
                    raise StateInvariantError(
                        f"resource reservation failed atomically: {exc}"
                    ) from exc
                self._record_internal_event(reservation_event)
                self._service_reservation_ids().append(queue_task.id)
                self._service_reservation_ids().sort()
                self._clear_queue_stall(facility.id)

                facility.resource_queue_agent_ids.pop(0)
                facility.resource_active_agent_ids.append(agent_id)
                active_task = ActiveTask(
                    id=queue_task.id,
                    kind="resource_service_water"
                    if resource_id == "water_ml"
                    else "resource_service_food",
                    agent_id=agent_id,
                    resource_id=facility.id,
                    start_minute=minute,
                    due_minute=due_minute,
                    staff_id=staff_member.id,
                )
                world.active_tasks[active_task.id] = active_task
                staff_member.active_task_id = active_task.id
                staff_member.work_queue[:] = [active_task.id]
                staff_member.busy_until = due_minute
                world.agents[agent_id].activity = Activity.IN_SERVICE
                world.agents[agent_id].target_id = facility.id
                world.schedule_event(completion_event)
                self._record_queue_wait(minute - queue_task.start_minute, resource_id)
                started.append(completion_event)
        if validate_state:
            world.validate()
        return tuple(started)

    def _arbitrate_independent_facility(
        self, facility: object, resource_id: str, minute: int
    ) -> list[ScheduledEvent]:
        world = self.world
        queue_state = world.model_state[FACILITY_QUEUES_STATE_KEY]
        started: list[ScheduledEvent] = []
        facility_limit = min(facility.capacity, facility.working_units)
        skill = self.config["water_staff_skill" if resource_id == "water_ml" else "meal_staff_skill"]
        quantity = self.config[
            "water_ml_per_service" if resource_id == "water_ml" else "food_meal_units_per_service"
        ]
        for queue_id in queue_ids_for_facility(world, facility.id):
            prepare_independent_facility_queue(
                world, queue_id, minute=minute, resource=True
            )
            queue = queue_state["queues"][queue_id]
            point = point_for_queue(world, queue_id)
            if point is None:
                raise StateInvariantError(f"resource queue {queue_id!r} lost its signed point")
            while queue["queued_request_ids"]:
                if progress_physical_queue_positions(
                    world, facility.id, resource=True, minute=minute, queue_id=queue_id
                ):
                    break
                request_id = queue["queued_request_ids"][0]
                request = queue_state["requests"][request_id]
                agent_id = request["agent_id"]
                queue_task = world.active_tasks.get(request["queue_task_id"])
                if queue_task is None or queue_task.kind != "resource_service_queue":
                    raise StateInvariantError("independent resource queue lost its active task")
                if resource_id == "water_ml" and water_service_blocked(world, facility.id):
                    mark_stall(world, queue_id, request_id, "outage")
                    self._record_queue_stall(facility.id, agent_id, "water_outage")
                    break
                point_capacity = physical_service_capacity(
                    world, facility.id, point.service_capacity, queue_id
                )
                if (
                    len(queue["active_request_ids"]) >= point_capacity
                    or len(facility.resource_active_agent_ids) >= facility_limit
                ):
                    mark_stall(world, queue_id, request_id, "capacity")
                    self._record_queue_stall(facility.id, agent_id, "capacity")
                    break
                required_skills = set(point.access.required_staff_skills)
                staff_member = next(
                    (
                        person for person in sorted(world.staff.values(), key=lambda item: item.id)
                        if person.zone_id == facility.zone_id
                        and skill in person.skills
                        and required_skills <= set(person.skills)
                        and person.active_task_id is None
                        and staff_is_available(world, person.id)
                        and staff_dynamics_is_available(world, person.id, minute)
                    ),
                    None,
                )
                if staff_member is None:
                    mark_stall(world, queue_id, request_id, "staff")
                    self._record_queue_stall(facility.id, agent_id, "staff")
                    break
                account = world.model_state[RESOURCE_LEDGER_KEY]["accounts"][resource_id]
                if account["remaining"] - account["reserved"] < quantity:
                    mark_stall(world, queue_id, request_id, "stock")
                    self._record_queue_stall(facility.id, agent_id, "stock")
                    break
                service_minutes = self.config["service_minutes"]
                due_minute = minute + service_minutes
                if due_minute >= world.duration_minutes:
                    mark_stall(world, queue_id, request_id, "run_end")
                    self._record_queue_stall(facility.id, agent_id, "run_end")
                    break
                if progress_physical_queue_head_to_service(
                    world, facility.id, resource=True, minute=minute, queue_id=queue_id
                ):
                    break
                active_task = ActiveTask(
                    id=queue_task.id,
                    kind="resource_service_water" if resource_id == "water_ml" else "resource_service_food",
                    agent_id=agent_id, resource_id=facility.id, start_minute=minute,
                    due_minute=due_minute, staff_id=staff_member.id,
                )
                completion_sequence = world.id_counters.get("event", 0)
                completion_event = ScheduledEvent(
                    event_id=f"resource-service-complete-{completion_sequence}",
                    kind=EventKind.COMPLETION, due_minute=due_minute, priority=0,
                    sequence=completion_sequence, event_type="resource_service_complete",
                    payload={
                        "facility_id": facility.id, "agent_id": agent_id,
                        "task_id": queue_task.id, "resource_id": resource_id,
                        "reservation_id": queue_task.id, "quantity": quantity,
                        "staff_id": staff_member.id,
                    },
                    source_id=queue_task.id, causation_id=queue_task.id,
                    provenance=self._provenance(
                        action="service_scheduled", service_minutes=service_minutes,
                        queue_id=queue_id,
                    ),
                )
                self._assert_service_task_id_available(queue_task.id)
                self._ensure_event_identity_free(completion_event)
                if world.allocate_event_sequence() != completion_sequence:
                    raise StateInvariantError("event sequence changed during resource preflight")
                reservation_event = self._audit_event(
                    "reserve",
                    {
                        "warehouse_id": self._warehouse_id(), "resource_id": resource_id,
                        "reservation_id": queue_task.id, "agent_id": agent_id,
                        "quantity": quantity,
                    },
                    causation_id=queue_task.id,
                )
                try:
                    apply_resource_operation(world, reservation_event, "reserve")
                except ValueError as exc:
                    raise StateInvariantError(f"resource reservation failed atomically: {exc}") from exc
                self._record_internal_event(reservation_event)
                self._service_reservation_ids().append(queue_task.id)
                self._service_reservation_ids().sort()
                if agent_id not in facility.resource_queue_agent_ids:
                    raise StateInvariantError("independent resource request is missing from facility projection")
                facility.resource_queue_agent_ids.remove(agent_id)
                facility.resource_active_agent_ids.append(agent_id)
                world.active_tasks[queue_task.id] = active_task
                staff_member.active_task_id = active_task.id
                staff_member.work_queue[:] = [active_task.id]
                staff_member.busy_until = due_minute
                world.agents[agent_id].activity = Activity.IN_SERVICE
                world.agents[agent_id].target_id = facility.id
                world.schedule_event(completion_event)
                active_request = start_facility_queue_service(world, request_id, active_task.id)
                record_queue_transition(
                    world, active_request, transition="service_started", task_id=active_task.id,
                    wait_minutes=minute - queue_task.start_minute,
                    causation_id=completion_event.event_id,
                )
                self._clear_queue_stall(facility.id)
                self._record_queue_wait(minute - queue_task.start_minute, resource_id)
                started.append(completion_event)
        return started

    def complete(self, event: ScheduledEvent) -> None:
        """Consume one completed reservation, relieve its need, and release staff/slot."""
        payload = event.payload
        task_id = payload.get("task_id")
        if not isinstance(task_id, str):
            raise EventApplicationError("resource completion requires a task_id")
        task = self.world.active_tasks.get(task_id)
        if task is None:
            cancelled = self._cancelled_task_ids()
            if task_id in cancelled:
                raise EventSuppressed(f"resource service task {task_id!r} was cancelled")
            raise EventApplicationError("resource completion does not match an active task")
        self._assert_service_event_matches_task(event, task)
        facility = self.world.facilities[task.resource_id or ""]
        agent = self.world.agents[task.agent_id or ""]
        staff_member = self.world.staff[task.staff_id or ""]
        resource_id = payload["resource_id"]
        consume_event = self._audit_event(
            "consume",
            {
                "warehouse_id": self._warehouse_id(),
                "resource_id": resource_id,
                "reservation_id": task.id,
                "agent_id": agent.id,
                "quantity": payload["quantity"],
            },
            causation_id=event.event_id,
        )
        try:
            apply_resource_operation(self.world, consume_event, "consume")
        except ValueError as exc:
            raise EventApplicationError(f"resource consume rejected: {exc}") from exc
        self._record_internal_event(consume_event)
        self._remove_service_reservation(task.id)

        if resource_id == "water_ml":
            agent.needs.hydration_need = max(
                0.0, agent.needs.hydration_need - self.config["hydration_relief"]
            )
            self._increment_metric("water_service_completed_count")
        else:
            agent.needs.hunger = max(0.0, agent.needs.hunger - self.config["hunger_relief"])
            self._increment_metric("meal_service_completed_count")
        queue_request = finish_facility_queue_service(self.world, task.id, status="completed")
        if queue_request is not None:
            record_queue_transition(
                self.world, queue_request, transition="completed", task_id=task.id,
                wait_minutes=task.start_minute - queue_request["requested_minute"],
                causation_id=event.event_id,
            )
        facility.resource_active_agent_ids.remove(agent.id)
        self._release_staff(staff_member)
        del self.world.active_tasks[task.id]
        agent.activity = Activity.IDLE
        agent.target_id = None
        self._increment_metric("resource_service_completed_count")

    def cancel(self, event: ScheduledEvent) -> None:
        """Cancel queued or active service; active inventory reservations are released once."""
        task_id = event.payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise EventApplicationError("resource cancellation requires a task_id")
        if set(event.payload) != {"task_id", "reason"} or event.payload.get("reason") not in {
            "operator_cancelled",
            "facility_stopped",
            "staff_unavailable",
        }:
            raise EventApplicationError("resource cancellation payload is not a typed cancellation")
        if task_id in self._cancelled_task_ids():
            raise EventSuppressed(f"resource service task {task_id!r} was already cancelled")
        task = self.world.active_tasks.get(task_id)
        if task is None or task.kind not in {
            "resource_service_queue",
            "resource_service_water",
            "resource_service_food",
        }:
            raise EventApplicationError("resource cancellation does not match an active task")
        agent = self.world.agents[task.agent_id or ""]
        facility = self.world.facilities[task.resource_id or ""]
        if task.kind == "resource_service_queue":
            if agent.id not in facility.resource_queue_agent_ids:
                raise EventApplicationError("queued resource Agent has no matching queue entry")
            facility.resource_queue_agent_ids.remove(agent.id)
            self._clear_queue_stall(facility.id)
        else:
            resource_id = self.resource_for_facility(facility.kind)
            if resource_id is None:
                raise EventApplicationError(
                    "resource service task references a non-resource facility"
                )
            if agent.id not in facility.resource_active_agent_ids:
                raise EventApplicationError("active resource Agent has no matching facility slot")
            staff_member = self.world.staff.get(task.staff_id or "")
            reservation = self.world.model_state[RESOURCE_LEDGER_KEY]["accounts"][resource_id][
                "reservations"
            ].get(task.id)
            if (
                staff_member is None
                or staff_member.active_task_id != task.id
                or staff_member.work_queue != [task.id]
                or not isinstance(reservation, dict)
                or reservation.get("agent_id") != agent.id
                or reservation.get("resource_id") != resource_id
                or task.id not in self._service_reservation_ids()
            ):
                raise EventApplicationError(
                    "active resource cancellation does not match reservation, slot, and staff"
                )
            release_event = self._audit_event(
                "release",
                {
                    "warehouse_id": self._warehouse_id(),
                    "resource_id": resource_id,
                    "reservation_id": task.id,
                    "agent_id": agent.id,
                },
                causation_id=event.event_id,
            )
            try:
                apply_resource_operation(self.world, release_event, "release")
            except ValueError as exc:
                raise EventApplicationError(
                    f"resource reservation release rejected: {exc}"
                ) from exc
            self._record_internal_event(release_event)
            self._remove_service_reservation(task.id)
            facility.resource_active_agent_ids.remove(agent.id)
            self._release_staff(staff_member)
        queue_request = finish_facility_queue_service(
            self.world, task_id, status="interrupted" if event.payload["reason"] == "staff_unavailable" else "cancelled"
        )
        if queue_request is not None:
            record_queue_transition(
                self.world, queue_request,
                transition="interrupted" if event.payload["reason"] == "staff_unavailable" else "cancelled",
                task_id=task_id, causation_id=event.event_id,
            )
        del self.world.active_tasks[task_id]
        agent.activity = Activity.IDLE
        agent.target_id = None
        self._cancelled_task_ids().append(task_id)
        self._increment_metric("resource_service_cancelled_count")

    def interrupt_for_staff(self, staff_id: str, *, causation_event: ScheduledEvent) -> None:
        """Cancel an interrupted service and put its Agent at the FIFO head."""
        tasks = [
            task
            for task in self.world.active_tasks.values()
            if task.staff_id == staff_id
            and task.kind in ("resource_service_water", "resource_service_food")
        ]
        if not tasks:
            return
        if len(tasks) != 1:
            raise EventApplicationError("unavailable staff owns multiple resource services")
        task = tasks[0]
        prior_queue_request = facility_queue_request_for_task(self.world, task.id)
        if task.agent_id is None or task.resource_id is None:
            raise EventApplicationError("interrupted resource service has incomplete ownership")
        agent = self.world.agents.get(task.agent_id)
        facility = self.world.facilities.get(task.resource_id)
        staff_member = self.world.staff.get(staff_id)
        if (
            agent is None
            or facility is None
            or staff_member is None
            or agent.zone_id != facility.zone_id
            or agent.activity is not Activity.IN_SERVICE
            or staff_member.active_task_id != task.id
            or staff_member.work_queue != [task.id]
            or staff_member.busy_until != task.due_minute
            or agent.id not in facility.resource_active_agent_ids
        ):
            raise EventApplicationError("interrupted resource task has inconsistent state")
        resource_id = self.resource_for_facility(facility.kind)
        if resource_id is None:
            raise EventApplicationError("interrupted resource service has an invalid facility")
        reservation = self.world.model_state[RESOURCE_LEDGER_KEY]["accounts"][resource_id][
            "reservations"
        ].get(task.id)
        if (
            not isinstance(reservation, dict)
            or reservation.get("agent_id") != agent.id
            or reservation.get("resource_id") != resource_id
            or task.id not in self._service_reservation_ids()
        ):
            raise EventApplicationError("interrupted service reservation is inconsistent")
        queue_counter = self.world.id_counters.get("resource_task", 0)
        queue_task_id, next_queue_counter = self._next_queue_task_id(queue_counter)
        previous_requests = self.world.model_state[SERVICE_STATE_KEY]["request_history"]
        queued_at = next(
            (
                record["minute"]
                for record in reversed(previous_requests)
                if record["agent_id"] == agent.id
                and record["facility_id"] == facility.id
                and record["outcome"] == "queued"
            ),
            self.world.minute_index,
        )
        sequence = self.world.id_counters.get("event", 0)
        cancellation = ScheduledEvent(
            event_id=f"resource-service-cancel-{sequence}",
            kind=EventKind.ENDOGENOUS,
            due_minute=self.world.minute_index,
            priority=0,
            sequence=sequence,
            event_type="resource_service_cancel",
            payload={"task_id": task.id, "reason": "staff_unavailable"},
            source_id=self.config["source_id"],
            causation_id=causation_event.event_id,
            provenance=self._provenance(action="staff_interrupted", staff_id=staff_id),
        )
        self._ensure_event_identity_free(cancellation)
        if self.world.allocate_event_sequence() != sequence:
            raise StateInvariantError("event sequence changed during service interruption")
        self.cancel(cancellation)
        self._record_internal_event(cancellation)
        agent.activity = Activity.QUEUED
        agent.target_id = facility.id
        queued_task = ActiveTask(
            id=queue_task_id,
            kind="resource_service_queue",
            agent_id=agent.id,
            resource_id=facility.id,
            start_minute=queued_at,
            # A queued task uses equal start/due timestamps as its stable arrival time.
            # Keeping the original timestamp preserves accumulated FIFO wait after an
            # interruption without changing the queue-task invariant.
            due_minute=queued_at,
        )
        self.world.id_counters["resource_task"] = next_queue_counter
        self.world.active_tasks[queue_task_id] = queued_task
        facility.resource_queue_agent_ids.insert(0, agent.id)
        if prior_queue_request is not None:
            enqueue_facility_queue(
                self.world,
                queue_id=prior_queue_request["queue_id"],
                request_id=queue_task_id,
                task_id=queue_task_id,
                agent_id=agent.id,
                service_kind=prior_queue_request["service_kind"],
                requested_minute=queued_at,
            )
            queue_row = self.world.model_state[FACILITY_QUEUES_STATE_KEY]["queues"][prior_queue_request["queue_id"]]
            queue_row["queued_request_ids"].remove(queue_task_id)
            queue_row["queued_request_ids"].insert(0, queue_task_id)
            record_queue_transition(
                self.world,
                self.world.model_state[FACILITY_QUEUES_STATE_KEY]["requests"][queue_task_id],
                transition="requeued", task_id=queue_task_id, causation_id=causation_event.event_id,
            )
        self._clear_queue_stall(facility.id)
        action = "fetch_water" if resource_id == "water_ml" else "collect_meal"
        self.record_request_outcome(
            agent_id=agent.id,
            action=action,
            outcome="queued",
            reason="arrived_at_facility",
            facility_id=facility.id,
            origin_zone_id=agent.zone_id,
            detail="service_requeued_after_staff_unavailable",
        )

    @staticmethod
    def resource_for_facility(kind: str) -> str | None:
        if kind == "water_station":
            return "water_ml"
        if kind == "meal_distribution":
            return "food_meal_units"
        return None

    def record_request_outcome(
        self,
        *,
        agent_id: str,
        action: str,
        outcome: str,
        reason: str,
        facility_id: str | None,
        origin_zone_id: str,
        detail: str | None = None,
    ) -> None:
        """Persist why an attempted resource action queued, moved, or was rejected."""
        self.world.model_state[SERVICE_STATE_KEY]["request_history"].append(
            {
                "minute": self.world.minute_index,
                "agent_id": agent_id,
                "action": action,
                "outcome": outcome,
                "reason": reason,
                "facility_id": facility_id,
                "origin_zone_id": origin_zone_id,
                "detail": detail,
            }
        )

    def _queue_task(self, facility_id: str, agent_id: str) -> ActiveTask:
        matches = [
            task
            for task in self.world.active_tasks.values()
            if task.kind == "resource_service_queue"
            and task.agent_id == agent_id
            and task.resource_id == facility_id
        ]
        if len(matches) != 1:
            raise StateInvariantError(
                f"queued Agent {agent_id!r} needs exactly one resource queue task"
            )
        return matches[0]

    def _next_queue_task_id(self, counter: int) -> tuple[str, int]:
        """Find an unused H02 task/reservation ID without colliding with H01 input."""
        while True:
            task_id = f"resource-service-task-{counter}"
            if task_id not in self.world.active_tasks and not self._reservation_id_is_in_use(
                task_id
            ):
                return task_id, counter + 1
            counter += 1

    def _assert_service_task_id_available(self, task_id: str) -> None:
        if self._reservation_id_is_in_use(task_id):
            raise StateInvariantError(
                f"resource service task ID {task_id!r} conflicts with an H01 reservation"
            )

    def _reservation_id_is_in_use(self, reservation_id: str) -> bool:
        ledger = self.world.model_state[RESOURCE_LEDGER_KEY]
        if reservation_id in ledger["used_reservation_ids"]:
            return True
        return any(
            event.event_type == "resource_reserve"
            and event.payload.get("reservation_id") == reservation_id
            for event in self.world.event_queue.pending()
        )

    def _agent_has_exclusive_task(self, agent_id: str) -> bool:
        return any(
            task.agent_id == agent_id
            for task in self.world.active_tasks.values()
            if task.kind
            in {
                "toilet_queue",
                "toilet_service",
                "resource_service_queue",
                "resource_service_water",
                "resource_service_food",
                "travel_to_toilet",
                "travel_to_resource",
                "travel_return",
            }
        ) or any(
            agent_id in facility.queue_agent_ids
            or agent_id in facility.active_agent_ids
            or agent_id in facility.resource_queue_agent_ids
            or agent_id in facility.resource_active_agent_ids
            for facility in self.world.facilities.values()
        )

    def _assert_service_event_matches_task(self, event: ScheduledEvent, task: ActiveTask) -> None:
        payload = event.payload
        if (
            event.kind is not EventKind.COMPLETION
            or event.event_type != "resource_service_complete"
            or set(payload)
            != {
                "facility_id",
                "agent_id",
                "task_id",
                "resource_id",
                "reservation_id",
                "quantity",
                "staff_id",
            }
        ):
            raise EventApplicationError("resource completion payload or event kind is invalid")
        facility_id = payload.get("facility_id")
        resource_id = payload.get("resource_id")
        if (
            task.kind not in ("resource_service_water", "resource_service_food")
            or task.due_minute != event.due_minute
            or payload.get("agent_id") != task.agent_id
            or payload.get("staff_id") != task.staff_id
            or facility_id != task.resource_id
            or payload.get("reservation_id") != task.id
            or ResourceServiceManager.resource_for_facility(
                self.world.facilities[task.resource_id or ""].kind
            )
            != resource_id
        ):
            raise EventApplicationError("resource completion does not match its active task")
        facility = self.world.facilities[task.resource_id or ""]
        staff_member = self.world.staff.get(task.staff_id or "")
        expected_quantity = self.config[
            "water_ml_per_service" if resource_id == "water_ml" else "food_meal_units_per_service"
        ]
        reservation = self.world.model_state[RESOURCE_LEDGER_KEY]["accounts"][resource_id][
            "reservations"
        ].get(task.id)
        if (
            task.agent_id not in facility.resource_active_agent_ids
            or staff_member is None
            or staff_member.active_task_id != task.id
            or staff_member.busy_until != task.due_minute
            or payload.get("quantity") != expected_quantity
            or not isinstance(payload.get("quantity"), int)
            or isinstance(payload.get("quantity"), bool)
            or not isinstance(reservation, dict)
            or reservation.get("reservation_id") != task.id
            or reservation.get("resource_id") != resource_id
            or reservation.get("agent_id") != task.agent_id
            or reservation.get("quantity") != expected_quantity
            or task.id not in self._service_reservation_ids()
        ):
            raise EventApplicationError(
                "resource service completion does not match its full reservation, slot, or staff"
            )

    def _audit_event(
        self,
        operation: str,
        payload: dict[str, object],
        *,
        causation_id: str,
    ) -> ScheduledEvent:
        while True:
            sequence = self.world.allocate_event_sequence()
            event = ScheduledEvent(
                event_id=f"resource-{operation}-{sequence}",
                kind=EventKind.ENDOGENOUS,
                due_minute=self.world.minute_index,
                priority=0,
                sequence=sequence,
                event_type=f"resource_{operation}",
                payload=payload,
                source_id=self.config["source_id"],
                causation_id=causation_id,
                provenance=self._provenance(action=operation),
            )
            if self._event_identity_is_free(event):
                return event

    def _ensure_event_identity_free(self, event: ScheduledEvent) -> None:
        if not self._event_identity_is_free(event):
            raise StateInvariantError(f"resource event identity {event.event_id!r} is not unique")

    def _event_identity_is_free(self, event: ScheduledEvent) -> bool:
        return not (
            any(
                pending.event_id == event.event_id or pending.sequence == event.sequence
                for pending in self.world.event_queue.pending()
            )
            or self.world.event_log.contains_id(event.event_id)
            or self.world.event_log.contains_sequence(event.sequence)
        )

    def _record_internal_event(self, event: ScheduledEvent) -> None:
        self.world.event_log.append(
            EventLogRecord(
                event=event,
                realized_minute=self.world.minute_index,
                status=EventStatus.APPLIED,
            )
        )

    def _provenance(self, *, action: str, **details: object) -> dict[str, object]:
        return {
            "source_type": self.config["source_type"],
            "source_id": self.config["source_id"],
            "policy_version": self.config["policy_version"],
            "action": action,
            **details,
        }

    def _warehouse_id(self) -> str:
        return self.world.model_state[RESOURCE_LEDGER_KEY]["warehouse_id"]

    def _cancelled_task_ids(self) -> list[str]:
        return self.world.model_state[SERVICE_STATE_KEY]["cancelled_task_ids"]

    def _service_reservation_ids(self) -> list[str]:
        return self.world.model_state[SERVICE_STATE_KEY]["active_reservation_ids"]

    def _remove_service_reservation(self, reservation_id: str) -> None:
        if reservation_id not in self._service_reservation_ids():
            raise StateInvariantError(
                f"resource service reservation {reservation_id!r} is not service-owned"
            )
        self._service_reservation_ids().remove(reservation_id)

    def _record_queue_stall(self, facility_id: str, agent_id: str, reason: str) -> None:
        state = self.world.model_state[SERVICE_STATE_KEY]
        previous = state["stall_state"].get(facility_id)
        if previous == {"agent_id": agent_id, "reason": reason}:
            return
        state["stall_state"][facility_id] = {"agent_id": agent_id, "reason": reason}
        state["queue_stall_history"].append(
            {
                "minute": self.world.minute_index,
                "facility_id": facility_id,
                "agent_id": agent_id,
                "reason": reason,
            }
        )
        self._increment_metric(f"resource_service_wait_{reason}_count")

    def _clear_queue_stall(self, facility_id: str) -> None:
        self.world.model_state[SERVICE_STATE_KEY]["stall_state"].pop(facility_id, None)

    def _release_staff(self, staff_member: object) -> None:
        staff_member.work_queue.clear()
        staff_member.active_task_id = None
        staff_member.busy_until = 0

    def _record_queue_wait(self, wait_minutes: int, resource_id: str) -> None:
        key = "water_service_wait" if resource_id == "water_ml" else "meal_service_wait"
        self.world.metric_state[f"{key}_count"] = (
            self.world.metric_state.get(f"{key}_count", 0.0) + 1.0
        )
        self.world.metric_state[f"{key}_total_minutes"] = (
            self.world.metric_state.get(f"{key}_total_minutes", 0.0) + wait_minutes
        )
        self.world.metric_state[f"{key}_max_minutes"] = max(
            self.world.metric_state.get(f"{key}_max_minutes", 0.0), float(wait_minutes)
        )

    def _increment_metric(self, key: str) -> None:
        self.world.metric_state[key] = self.world.metric_state.get(key, 0.0) + 1.0
