"""Independent checkpointed FIFO state for explicitly distinct service queues."""

from __future__ import annotations

from typing import Any

from sheltersim.contracts.extensions import FacilityServicePoint
from sheltersim.core.world import WorldState
from sheltersim.domain.geometry_overlay import parsed_facility_geometry
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent

FACILITY_QUEUES_STATE_KEY = "facility_queues_v1"
FACILITY_QUEUES_POLICY_VERSION = "independent-facility-queues-v1"
FACILITY_QUEUES_LEGACY_MODE_KEY = "facility_queues_legacy_mode_v1"
FACILITY_QUEUES_LEGACY_MODE = {
    "schema_version": 1,
    "mode": "pre_independent_queue_checkpoint",
    "policy_version": "facility_fifo_legacy-v1",
}
MAX_QUEUE_REQUESTS = 100_000


def service_points(world: WorldState) -> tuple[FacilityServicePoint, ...]:
    raw = world.model_configs.get("facility_geometry")
    if not isinstance(raw, dict):
        return ()
    return parsed_facility_geometry(raw).service_points


def independent_queues_required(configs: dict[str, Any]) -> bool:
    """Opt in only when queue IDs change legacy one-FIFO-per-facility semantics."""
    raw = configs.get("facility_geometry")
    if not isinstance(raw, dict):
        return False
    points = parsed_facility_geometry(raw).service_points
    by_facility: dict[str, int] = {}
    for point in points:
        by_facility[point.facility_id] = by_facility.get(point.facility_id, 0) + 1
    return any(point.queue_id != point.facility_id for point in points) or any(
        count > 1 for count in by_facility.values()
    )


def is_legacy_queue_checkpoint(world: WorldState) -> bool:
    return (
        independent_queues_required(world.model_configs)
        and world.model_state.get(FACILITY_QUEUES_STATE_KEY) is None
        and world.model_state.get(FACILITY_QUEUES_LEGACY_MODE_KEY) == FACILITY_QUEUES_LEGACY_MODE
    )


def create_facility_queue_state(world: WorldState) -> dict[str, Any]:
    points = service_points(world)
    if not independent_queues_required(world.model_configs):
        raise ValueError("facility_queues_v1 requires independent explicit service points")
    return {
        "schema_version": 1,
        "policy_version": FACILITY_QUEUES_POLICY_VERSION,
        "queues": {
            point.queue_id: {
                "facility_id": point.facility_id,
                "service_point_id": point.id,
                "queued_request_ids": [],
                "active_request_ids": [],
                "last_stall": None,
            }
            for point in sorted(points, key=lambda row: row.queue_id)
        },
        "requests": {},
    }


def validate_facility_queue_state(world: WorldState) -> None:
    state = world.model_state.get(FACILITY_QUEUES_STATE_KEY)
    required = independent_queues_required(world.model_configs)
    if state is None:
        if required and not is_legacy_queue_checkpoint(world):
            raise ValueError("independent service points require facility_queues_v1 state")
        return
    if FACILITY_QUEUES_LEGACY_MODE_KEY in world.model_state:
        raise ValueError("legacy facility queue mode cannot be mixed with independent queue state")
    if not required:
        raise ValueError("facility_queues_v1 has no matching independent service-point config")
    if (
        not isinstance(state, dict)
        or set(state) != {"schema_version", "policy_version", "queues", "requests"}
        or state.get("schema_version") != 1
        or state.get("policy_version") != FACILITY_QUEUES_POLICY_VERSION
        or not isinstance(state.get("queues"), dict)
        or not isinstance(state.get("requests"), dict)
        or len(state["requests"]) > MAX_QUEUE_REQUESTS
    ):
        raise ValueError("facility_queues_v1 has an invalid exact schema")
    expected = {point.queue_id: point for point in service_points(world)}
    if set(state["queues"]) != set(expected):
        raise ValueError("facility_queues_v1 queue IDs do not match the signed service points")
    active_agents: set[str] = set()
    for queue_id, row in state["queues"].items():
        point = expected[queue_id]
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "facility_id",
                "service_point_id",
                "queued_request_ids",
                "active_request_ids",
                "last_stall",
            }
            or row["facility_id"] != point.facility_id
            or row["service_point_id"] != point.id
            or not isinstance(row["queued_request_ids"], list)
            or not isinstance(row["active_request_ids"], list)
            or len(row["queued_request_ids"]) != len(set(row["queued_request_ids"]))
            or len(row["active_request_ids"]) != len(set(row["active_request_ids"]))
            or set(row["queued_request_ids"]) & set(row["active_request_ids"])
        ):
            raise ValueError(f"facility queue {queue_id!r} is invalid")
        if len(row["active_request_ids"]) > point.service_capacity:
            raise ValueError(f"facility queue {queue_id!r} exceeds its signed concurrent capacity")
        stall = row["last_stall"]
        if stall is not None and (
            not isinstance(stall, dict)
            or set(stall) != {"minute", "request_id", "reason"}
            or type(stall["minute"]) is not int
            or stall["minute"] < 0
            or not isinstance(stall["request_id"], str)
            or stall["reason"] not in {"capacity", "staff", "stock", "outage", "route", "run_end"}
        ):
            raise ValueError(f"facility queue {queue_id!r} stall state is invalid")
        for request_id in (*row["queued_request_ids"], *row["active_request_ids"]):
            request = state["requests"].get(request_id)
            if (
                not isinstance(request, dict)
                or request.get("request_id") != request_id
                or request.get("queue_id") != queue_id
                or request.get("facility_id") != point.facility_id
                or request.get("service_point_id") != point.id
                or request.get("agent_id") not in world.agents
                or request.get("service_kind") not in {"toilet", "water", "meal"}
                or type(request.get("requested_minute")) is not int
                or not 0 <= request["requested_minute"] <= world.minute_index
                or request.get("status")
                != ("queued" if request_id in row["queued_request_ids"] else "active")
                or not isinstance(request.get("queue_task_id"), str)
                or not isinstance(request.get("active_task_id"), str)
            ):
                raise ValueError(f"facility queue request {request_id!r} is invalid")
            agent_id = request["agent_id"]
            if agent_id in active_agents:
                raise ValueError("an Agent cannot occupy two independent service queues")
            active_agents.add(agent_id)
            facility = world.facilities[point.facility_id]
            agent = world.agents[agent_id]
            expected_kind = (
                "water_station"
                if request["service_kind"] == "water"
                else "meal_distribution"
                if request["service_kind"] == "meal"
                else None
            )
            if expected_kind is not None and facility.kind != expected_kind:
                raise ValueError("facility queue service kind does not match its resource facility")
            if request_id in row["queued_request_ids"]:
                task = world.active_tasks.get(request["queue_task_id"])
                if (
                    task is None
                    or task.kind not in {"toilet_queue", "resource_service_queue"}
                    or task.agent_id != agent_id
                    or task.resource_id != point.facility_id
                    or agent.activity.value != "queued"
                    or agent.target_id != point.facility_id
                ):
                    raise ValueError("queued service request has no matching task and Agent state")
                mirror = (
                    facility.resource_queue_agent_ids
                    if request["service_kind"] in {"water", "meal"}
                    else facility.queue_agent_ids
                )
                if agent_id not in mirror:
                    raise ValueError(
                        "independent queue request is missing from the facility projection"
                    )
            else:
                task = world.active_tasks.get(request["active_task_id"])
                if (
                    task is None
                    or task.agent_id != agent_id
                    or task.resource_id != point.facility_id
                    or task.kind
                    not in {"toilet_service", "resource_service_water", "resource_service_food"}
                    or agent.activity.value != "in_service"
                    or agent.target_id != point.facility_id
                ):
                    raise ValueError(
                        "active service request has no matching service task and Agent state"
                    )
                if request["service_kind"] == "toilet" and (
                    point.access.required_staff_skills or task.staff_id is not None
                ):
                    staff_member = world.staff.get(task.staff_id or "")
                    if (
                        not point.access.required_staff_skills
                        or staff_member is None
                        or not set(point.access.required_staff_skills) <= set(staff_member.skills)
                        or staff_member.zone_id != world.facilities[point.facility_id].zone_id
                        or staff_member.active_task_id != task.id
                        or staff_member.work_queue != [task.id]
                        or staff_member.busy_until != task.due_minute
                    ):
                        raise ValueError(
                            "staff-required toilet service has no matching skilled staff "
                            "reservation"
                        )
                mirror = (
                    facility.resource_active_agent_ids
                    if request["service_kind"] in {"water", "meal"}
                    else facility.active_agent_ids
                )
                if agent_id not in mirror:
                    raise ValueError(
                        "active independent service is missing from the facility projection"
                    )
    if set(state["requests"]) != {
        request_id
        for row in state["queues"].values()
        for request_id in (*row["queued_request_ids"], *row["active_request_ids"])
    }:
        raise ValueError("facility_queues_v1 contains an unassigned or terminal request")
    movement = world.model_state.get("movement_v1", {})
    journeys = movement.get("journeys", {}) if isinstance(movement, dict) else {}
    for agent_id, journey in journeys.items():
        queue_id = journey.get("queue_id")
        if queue_id is None:
            continue
        request = request_for_agent(world, agent_id, queue_id=queue_id)
        if request is None or request["status"] != "queued":
            raise ValueError("queue movement journey has no matching queued request")


def choose_queue_id(world: WorldState, facility_id: str, agent_id: str) -> str | None:
    """Choose an accessible queue by current load and stable queue ID."""
    state = world.model_state.get(FACILITY_QUEUES_STATE_KEY)
    if not isinstance(state, dict):
        return None
    traits = world.model_configs.get("population", {}).get("people", {}).get(agent_id, {})
    access_tags = set(traits.get("accessibility_needs", ()))
    agent = world.agents.get(agent_id)
    if agent is not None:
        access_tags.add(agent.profile.mobility.value)
    candidates: list[tuple[int, str]] = []
    for point in service_points(world):
        if point.facility_id != facility_id:
            continue
        if (
            point.access.accessibility == "restricted"
            and not set(point.access.required_access_tags) <= access_tags
        ):
            continue
        queue = state["queues"][point.queue_id]
        candidates.append(
            (len(queue["queued_request_ids"]) + len(queue["active_request_ids"]), point.queue_id)
        )
    return min(candidates)[1] if candidates else None


def point_for_queue(world: WorldState, queue_id: str) -> FacilityServicePoint | None:
    return next((point for point in service_points(world) if point.queue_id == queue_id), None)


def queue_ids_for_facility(world: WorldState, facility_id: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            point.queue_id for point in service_points(world) if point.facility_id == facility_id
        )
    )


def facility_has_independent_queues(world: WorldState, facility_id: str) -> bool:
    state = world.model_state.get(FACILITY_QUEUES_STATE_KEY)
    return isinstance(state, dict) and any(
        row.get("facility_id") == facility_id for row in state.get("queues", {}).values()
    )


def queue_head_agent_ids(world: WorldState, facility_id: str) -> set[str]:
    """The Agents first in line at a facility: one per independent queue, else the shared head."""
    state = world.model_state.get(FACILITY_QUEUES_STATE_KEY)
    if facility_has_independent_queues(world, facility_id):
        return {
            state["requests"][row["queued_request_ids"][0]]["agent_id"]
            for row in state["queues"].values()
            if row["facility_id"] == facility_id and row["queued_request_ids"]
        }
    return set(world.facilities[facility_id].resource_queue_agent_ids[:1])


def request_for_task(world: WorldState, task_id: str) -> dict[str, Any] | None:
    state = world.model_state.get(FACILITY_QUEUES_STATE_KEY)
    request = state.get("requests", {}).get(task_id) if isinstance(state, dict) else None
    if request is not None:
        return request
    if isinstance(state, dict):
        return next(
            (row for row in state["requests"].values() if row["active_task_id"] == task_id), None
        )
    return None


def request_for_agent(
    world: WorldState, agent_id: str, *, queue_id: str | None = None
) -> dict[str, Any] | None:
    """Return the live queue reservation owned by an Agent."""
    state = world.model_state.get(FACILITY_QUEUES_STATE_KEY)
    if not isinstance(state, dict):
        return None
    return next(
        (
            row
            for row in state["requests"].values()
            if row["agent_id"] == agent_id and (queue_id is None or row["queue_id"] == queue_id)
        ),
        None,
    )


def queue_agent_ids(world: WorldState, queue_id: str) -> list[str]:
    state = world.model_state.get(FACILITY_QUEUES_STATE_KEY)
    if not isinstance(state, dict) or queue_id not in state["queues"]:
        return []
    return [
        state["requests"][request_id]["agent_id"]
        for request_id in state["queues"][queue_id]["queued_request_ids"]
    ]


def enqueue(
    world: WorldState,
    *,
    queue_id: str,
    request_id: str,
    task_id: str,
    agent_id: str,
    service_kind: str,
    requested_minute: int,
) -> None:
    state = world.model_state.get(FACILITY_QUEUES_STATE_KEY)
    point = point_for_queue(world, queue_id)
    if not isinstance(state, dict) or point is None or request_id in state["requests"]:
        raise ValueError("service queue is not installed or request ID is already in use")
    state["requests"][request_id] = {
        "request_id": request_id,
        "queue_id": queue_id,
        "service_point_id": point.id,
        "facility_id": point.facility_id,
        "agent_id": agent_id,
        "service_kind": service_kind,
        "requested_minute": requested_minute,
        "status": "queued",
        "queue_task_id": task_id,
        "active_task_id": task_id,
    }
    state["queues"][queue_id]["queued_request_ids"].append(request_id)


def start_service(world: WorldState, request_id: str, active_task_id: str) -> dict[str, Any]:
    state = world.model_state[FACILITY_QUEUES_STATE_KEY]
    request = state["requests"][request_id]
    queue = state["queues"][request["queue_id"]]
    queue["queued_request_ids"].remove(request_id)
    queue["active_request_ids"].append(request_id)
    request["status"] = "active"
    request["active_task_id"] = active_task_id
    queue["last_stall"] = None
    return request


def finish_service(world: WorldState, task_id: str, *, status: str) -> dict[str, Any] | None:
    state = world.model_state.get(FACILITY_QUEUES_STATE_KEY)
    if not isinstance(state, dict):
        return None
    request = request_for_task(world, task_id)
    if request is None:
        return None
    queue = state["queues"][request["queue_id"]]
    collection = "queued_request_ids" if request["status"] == "queued" else "active_request_ids"
    if request["request_id"] in queue[collection]:
        queue[collection].remove(request["request_id"])
    request = dict(request)
    request["status"] = status
    state["requests"].pop(request["request_id"], None)
    queue["last_stall"] = None
    return request


def mark_stall(world: WorldState, queue_id: str, request_id: str, reason: str) -> None:
    state = world.model_state.get(FACILITY_QUEUES_STATE_KEY)
    if not isinstance(state, dict) or queue_id not in state["queues"]:
        return
    state["queues"][queue_id]["last_stall"] = {
        "minute": world.minute_index,
        "request_id": request_id,
        "reason": reason,
    }


def record_queue_transition(
    world: WorldState,
    request: dict[str, Any],
    *,
    transition: str,
    task_id: str,
    wait_minutes: int | None = None,
    causation_id: str | None = None,
) -> None:
    """Persist typed queue evidence into EventLog without inventing a scheduled event."""
    sequence = world.allocate_event_sequence()
    point = point_for_queue(world, request["queue_id"])
    queue_row = world.model_state[FACILITY_QUEUES_STATE_KEY]["queues"][request["queue_id"]]
    queued_ids = queue_row["queued_request_ids"]
    payload = {
        "schema_version": 1,
        "record_type": "operations",
        "event_type": "facility_queue_transition",
        "event_id": f"facility-queue-transition-{sequence}",
        "request_id": request["request_id"],
        "queue_id": request["queue_id"],
        "service_point_id": request["service_point_id"],
        "facility_id": request["facility_id"],
        "agent_id": request["agent_id"],
        "service_kind": request["service_kind"],
        "transition": transition,
        "minute": world.minute_index,
        "requested_minute": request["requested_minute"],
        "task_id": task_id,
        "queue_position": (
            queued_ids.index(request["request_id"]) + 1
            if request["request_id"] in queued_ids
            else None
        ),
        "service_capacity_concurrent": point.service_capacity if point is not None else None,
        "wait_minutes": wait_minutes,
        "causation_id": causation_id,
    }
    event = ScheduledEvent(
        event_id=f"facility-queue-transition-{sequence}",
        kind=EventKind.ENDOGENOUS,
        due_minute=world.minute_index,
        priority=0,
        sequence=sequence,
        event_type="facility_queue_transition",
        payload=payload,
        source_id=FACILITY_QUEUES_POLICY_VERSION,
        causation_id=causation_id,
        provenance={
            "source_type": "assumption",
            "source_id": "ASM-FACILITY-QUEUES-V1",
            "policy_version": FACILITY_QUEUES_POLICY_VERSION,
        },
    )
    from sheltersim.contracts.operations import FacilityQueueTransitionReceipt

    FacilityQueueTransitionReceipt.model_validate(payload)
    world.event_log.append(
        EventLogRecord(event=event, realized_minute=world.minute_index, status=EventStatus.APPLIED)
    )
