"""Read-only presentation of one authoritative boundary state, shared by both APIs."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any

from sheltersim.contracts.api import AgentPosition, TimelineFrame
from sheltersim.contracts.extensions import Point2D, point_in_polygon
from sheltersim.contracts.population import population_context_people
from sheltersim.core.world import WorldState
from sheltersim.domain.care_receipts import care_history_by_agent, care_history_for_agent
from sheltersim.domain.population_lifecycle import current_household_id
from sheltersim.domain.resources import resource_metric_values
from sheltersim.domain.staff import support_wait_reason_codes, support_waited_minutes
from sheltersim.experiments.metrics import MetricSnapshot


def world_frame(
    world: WorldState,
    *,
    run_id: str,
    branch_id: str,
    computed_until: int,
    state_hash: str | None = None,
) -> dict[str, Any]:
    """No state initialization, policy evaluation, RNG draws or provider capability.

    A caller that already holds the digest of ``world.to_dict()`` (a checkpoint's
    ``state_hash``) may pass it to avoid serializing the whole history again.
    """
    from sheltersim.domain.geometry_overlay import effective_geometry, effective_spatial_constraints

    geometry = effective_geometry(world)
    movement = world.model_state.get("movement_v1", {})
    positions = movement.get("positions", {})
    traits = world.model_state.get("population_dynamics_v1", {}).get(
        "profile_traits", world.model_configs.get("population", {}).get("people", {})
    )
    memory = world.model_state.get("agent_dynamics_v1", {}).get("agents", {})
    staff_memory = world.model_state.get("staff_dynamics_v1", {}).get("staff", {})
    beliefs = world.model_state.get("information_spread_v1", {}).get("beliefs", {})
    care_requests = world.model_state.get("shared_operations_v1", {}).get("support_requests", {})
    care_histories = care_history_by_agent(world)
    context_people = population_context_people(world)
    agents = []
    people = []
    social_events, social_window_truncated = social_event_records(world)
    group_zones: dict[str, set[str]] = {}
    for current_agent in world.agents.values():
        if current_household_id(world, current_agent.id) is not None:
            group_zones.setdefault(current_household_id(world, current_agent.id), set()).add(
                current_agent.zone_id
            )

    for agent in sorted(world.agents.values(), key=lambda a: a.id):
        record = positions.get(agent.id, {})
        point = Point2D.model_validate(record["point"]) if record.get("point") is not None else None
        kind = record.get("position_kind", "unavailable")
        zone = world.zones[agent.zone_id]
        if (
            point is None
            and geometry is None
            and zone.display_x is not None
            and zone.display_y is not None
        ):
            point, kind = Point2D(x=zone.display_x, y=zone.display_y), "display_only"
        agents.append(
            AgentPosition(
                agent_id=agent.id,
                zone_id=record.get("zone_id", agent.zone_id),
                point=point,
                edge_id=record.get("edge_id"),
                progress=record.get("progress", 0),
                position_kind=kind,
            )
        )
        dialogue = [
            item
            for item in social_events
            if agent.id in item["actors"]
            or (
                item["event_type"] == "information_correction"
                and item.get("zone_id") == agent.zone_id
            )
        ][-20:]
        household_zones = group_zones.get(current_household_id(world, agent.id) or "", set())
        active_care_requests = []
        if isinstance(care_requests, dict):
            for request_id, care_request in care_requests.items():
                if care_request.get("agent_id") != agent.id:
                    continue
                projected = deepcopy(care_request)
                support_request = world.support_requests.get(request_id)
                projected["assigned_staff_id"] = (
                    support_request.assigned_staff_id if support_request is not None else None
                )
                projected["runtime_status"] = (
                    support_request.status.value if support_request is not None else None
                )
                if support_request is not None:
                    projected["waited_minutes"] = support_waited_minutes(
                        world, request_id, minute=world.minute_index
                    )
                    projected["wait_reason_codes"] = list(
                        support_wait_reason_codes(world, request_id, minute=world.minute_index)
                    )
                active_care_requests.append(projected)
        people.append(
            {
                "id": agent.id,
                "group_id": agent.profile.group_id,
                "zone_id": agent.zone_id,
                "family_separated": bool(household_zones - {agent.zone_id}),
                "activity": agent.activity.value,
                "target_id": agent.target_id,
                "stress": agent.stress,
                "trust": agent.trust,
                "needs": vars(agent.needs).copy(),
                "mobility": agent.profile.mobility.value,
                "support_need": agent.profile.support_need,
                "traits": traits.get(agent.id),
                "context": deepcopy(context_people.get(agent.id))
                if isinstance(context_people, dict)
                else None,
                "care_requests": active_care_requests,
                "care_history": care_histories.get(agent.id, []),
                "decision": memory.get(agent.id),
                "beliefs": dict(list(beliefs.get(agent.id, {}).items())[:16]),
                "dialogue": dialogue,
                "dialogue_status": "structured_events_only" if dialogue else "no_retained_events",
            }
        )
    kpis = MetricSnapshot.from_world(world).csv_row()
    kpis.pop("source_ids_json", None)
    kpis.update(resource_metric_values(world) or {})
    pending = world.model_state.get("population_dynamics_v1", {}).get("pending_arrivals", [])
    approach_routes = {
        item.route_id: item for item in (geometry.arrival_approach_routes if geometry else ())
    }
    incoming_arrivals = []
    for item in pending:
        route_id = item.get("approach_route_id")
        route = approach_routes.get(route_id)
        if route is None:
            continue
        elapsed_minutes = max(0, world.minute_index - item["approach_started_minute"])
        remaining = min(route.distance_m, elapsed_minutes * route.walking_speed_m_per_min)
        progress = remaining / route.distance_m
        position = route.points_m[-1]
        for index, (left, right) in enumerate(
            zip(route.points_m, route.points_m[1:], strict=False)
        ):
            segment = math.hypot(right.x - left.x, right.y - left.y)
            if remaining <= segment or index == len(route.points_m) - 2:
                fraction = 1.0 if segment <= 0 else min(1.0, remaining / segment)
                position = Point2D(
                    x=left.x + (right.x - left.x) * fraction,
                    y=left.y + (right.y - left.y) * fraction,
                )
                break
            remaining -= segment
        incoming_arrivals.append(
            {
                "agent_id": item["profile"]["id"],
                "route_id": route.route_id,
                "destination_zone_id": route.destination_zone_id,
                "position": position.model_dump(mode="json"),
                "progress": progress,
                "status": (
                    "awaiting_admission"
                    if item.get("approach_completed_minute") is not None
                    else "censored"
                    if world.minute_index >= world.duration_minutes
                    and item["approach_due_minute"] >= world.duration_minutes
                    else "approaching"
                ),
                "arrival_minute": item["approach_due_minute"],
            }
        )
    capacity = sum(
        zone.capacity or 0
        for key, zone in world.zones.items()
        if key == "sleeping" or key.startswith("sleeping_")
    )
    kpis.update(
        {
            "population": len(world.agents),
            "pending_arrivals": len(pending),
            "sleeping_capacity": capacity,
            "occupancy_ratio": len(world.agents) / capacity if capacity else None,
        }
    )
    events = [
        {
            "event_id": r.event.event_id,
            "minute": r.realized_minute,
            "event_type": r.event.event_type,
            "kind": r.event.kind.value,
            "status": r.status.value,
            "source_id": r.event.source_id,
            "causation_id": r.event.causation_id,
        }
        for r in world.event_log.recent(100)
    ]
    frame = TimelineFrame(
        run_id=run_id,
        branch_id=branch_id,
        minute=world.minute_index,
        computed_until=computed_until,
        state_hash=state_hash if state_hash is not None else world.state_digest(),
        agents=tuple(agents),
        kpis=kpis,
        events=tuple(events),
    ).model_dump(mode="json")
    zone_geometry = {z.zone_id: z.polygon for z in geometry.zones} if geometry else {}
    facilities = []
    independent_queues = world.model_state.get("facility_queues_v1", {})
    for f in sorted(world.facilities.values(), key=lambda f: f.id):
        point = geometry.facility_points.get(f.id) if geometry else None
        if point is not None and (
            f.zone_id not in zone_geometry or not point_in_polygon(point, zone_geometry[f.zone_id])
        ):
            point = None
        service_queues = []
        if geometry is not None:
            for service_point in geometry.service_points:
                if service_point.facility_id != f.id:
                    continue
                queue = independent_queues.get("queues", {}).get(service_point.queue_id, {})
                requests = independent_queues.get("requests", {})
                service_queues.append(
                    {
                        "service_point_id": service_point.id,
                        "queue_id": service_point.queue_id,
                        "point": service_point.point_m.model_dump(mode="json"),
                        "queue_agent_ids": [
                            requests[item]["agent_id"]
                            for item in queue.get("queued_request_ids", [])
                            if item in requests
                        ],
                        "active_agent_ids": [
                            requests[item]["agent_id"]
                            for item in queue.get("active_request_ids", [])
                            if item in requests
                        ],
                        "queued_request_ids": list(queue.get("queued_request_ids", [])),
                        "active_request_ids": list(queue.get("active_request_ids", [])),
                        "service_capacity_concurrent": service_point.service_capacity,
                        "accessibility": service_point.access.accessibility,
                        "required_access_tags": list(service_point.access.required_access_tags),
                        "required_staff_skills": list(service_point.access.required_staff_skills),
                        "last_stall": deepcopy(queue.get("last_stall")),
                    }
                )
        facilities.append(
            {
                "id": f.id,
                "kind": f.kind,
                "zone_id": f.zone_id,
                "capacity": f.capacity,
                "working_units": f.working_units,
                "point": point.model_dump() if point else None,
                "queue_agent_ids": list(f.queue_agent_ids),
                "active_agent_ids": list(f.active_agent_ids),
                "resource_queue_agent_ids": list(f.resource_queue_agent_ids),
                "resource_active_agent_ids": list(f.resource_active_agent_ids),
                "service_queues": service_queues,
            }
        )
    relationships = world.model_state.get("social_network_v1", {}).get("relationships", {})
    geometry_payload = geometry.model_dump(mode="json") if geometry else None
    if geometry_payload is not None and not geometry.arrival_approach_routes:
        geometry_payload.pop("arrival_approach_routes", None)
    frame.update(
        {
            "people": people,
            "facilities": facilities,
            "staff": [
                {
                    "id": s.id,
                    "zone_id": s.zone_id,
                    "skills": list(s.skills),
                    "active_task_id": s.active_task_id,
                    "position": movement.get("staff_positions", {}).get(s.id),
                    "dynamics": staff_memory.get(s.id),
                }
                for s in sorted(world.staff.values(), key=lambda s: s.id)
            ],
            "geometry": geometry_payload,
            "zones": [
                {"id": z.id, "name": z.name, "capacity": z.capacity} for z in world.zones.values()
            ],
            "social": {
                "edges": [
                    {"pair": json.loads(key), **value}
                    for key, value in list(sorted(relationships.items()))[:2000]
                ],
                "total_edges": len(relationships),
                "truncated": len(relationships) > 2000,
                "interactions": social_events,
            },
            "movement": {
                "edge_usage_agent_minutes": dict(movement.get("edge_usage_minutes", {})),
                "journey_count": len(movement.get("journeys", {})),
                "active_journeys": [
                    {"agent_id": agent_id, **journey}
                    for agent_id, journey in sorted(movement.get("journeys", {}).items())
                ],
            },
            "pending_arrivals": len(pending),
            "incoming_arrivals": incoming_arrivals,
            "kpi_time_semantics": "state at boundary before this tick",
            "position_limitations": [
                "Staff travel follows the installed zone route model; in-room obstacle "
                "avoidance and shared doorway arbitration remain approximations.",
                "Missing physical coordinates are not generated as walking trajectories.",
                "Arrival approach coordinates and walking speeds are explicit operator or "
                "source inputs, not inferred from a map.",
            ],
        }
    )
    from sheltersim.contracts.spatial import occupancy_snapshot

    body = world.model_state.get("body_state_v1", {})
    operations = world.model_state.get("staff_operations_v1", {})
    for person in frame["people"]:
        person["body"] = deepcopy(body.get("agents", {}).get(person["id"]))
    for staff in frame["staff"]:
        staff["operation"] = deepcopy(operations.get("staff", {}).get(staff["id"]))
    frame["presence"] = deepcopy(
        {
            key: value
            for key, value in world.model_state.get("population_presence_v1", {}).items()
            if key != "history"
        }
    )
    # History is fetched through its index, never duplicated in every frame.
    frame["presence"].pop("history", None)
    frame["sanitation"] = deepcopy(operations.get("zones", {}))
    frame["recorded_history"] = {
        "decisions": {
            "source": "people[].decision",
            "retention": "latest selected action per active agent",
        },
        "social": {
            "retained_records": len(body.get("history", [])),
            "event_window_truncated": social_window_truncated,
            "frame_event_scan_limit": 5000,
            "retention_limit": 20000,
            "recent_effects": deepcopy(body.get("history", [])[-200:]),
        },
        "staff": {
            "retained_records": len(operations.get("history", [])),
            "retention_limit": 20000,
            "recent_operations": deepcopy(operations.get("history", [])[-200:]),
        },
        "status": "bounded retained evidence; absence does not prove no earlier event",
    }
    frame["occupancy"] = occupancy_snapshot(world)
    from sheltersim.domain.common_operations import operational_demand_snapshot

    frame["operational_demand"] = operational_demand_snapshot(world)
    common_operations = world.model_state.get("shared_operations_v1", {})
    frame["recorded_history"]["shared_operations"] = {
        "contract_enabled": "shared_operations_v1" in world.model_state,
        "retained_records": len(common_operations.get("history", [])),
        "retention_limit": 20000,
        "read_route": "branch/history?kind=care or kind=operations",
        "claim_boundary": (
            "only event-linked applied transitions are indexed; this does not imply an "
            "unrecorded outcome"
        ),
    }
    frame["spatial_constraints"] = effective_spatial_constraints(world).model_dump(mode="json")
    frame["geometry_overlay"] = deepcopy(
        {
            key: value
            for key, value in world.model_state.get("geometry_overlay_v1", {}).items()
            if key != "history"
        }
    )
    overlay_rooms = frame["geometry_overlay"].get("temporary_rooms", {})
    if isinstance(overlay_rooms, dict):
        bed_reservations = frame["geometry_overlay"].get("bed_reservations", {})
        room_admissions = frame["geometry_overlay"].get("room_admissions", {})
        for room in overlay_rooms.values():
            polygon = tuple(Point2D.model_validate(point) for point in room["polygon_m"])
            occupants = []
            for agent_id, agent in world.agents.items():
                position = positions.get(agent_id, {})
                if (
                    agent.zone_id == room["zone_id"]
                    and position.get("point") is not None
                    and point_in_polygon(Point2D.model_validate(position["point"]), polygon)
                ):
                    occupants.append(agent_id)
            room["observed_occupant_agent_ids"] = sorted(occupants)
            occupied_beds = []
            claimed_agents: set[str] = set()
            for index, bed_point in enumerate(room["bed_points_m"]):
                bed = Point2D.model_validate(bed_point)
                nearby = []
                for agent_id in occupants:
                    position = Point2D.model_validate(positions[agent_id]["point"])
                    distance_squared = (position.x - bed.x) ** 2 + (position.y - bed.y) ** 2
                    if distance_squared <= 0.75**2 and agent_id not in claimed_agents:
                        nearby.append(agent_id)
                if nearby:
                    agent_id = min(nearby)
                    claimed_agents.add(agent_id)
                    occupied_beds.append({"bed_index": index, "agent_id": agent_id})
            room["observed_occupied_beds"] = occupied_beds
            room["reserved_beds"] = [
                {"bed_index": reservation["bed_index"], "agent_id": reservation["agent_id"]}
                for reservation in sorted(
                    bed_reservations.values(),
                    key=lambda item: (item["room_id"], item["bed_index"], item["agent_id"]),
                )
                if reservation["room_id"] == room["room_id"]
            ]
            room["admitted_agent_ids"] = sorted(
                admission["agent_id"]
                for admission in room_admissions.values()
                if admission["room_id"] == room["room_id"]
            )
            room["occupancy_basis"] = (
                "occupied beds are coordinate observations within 0.75 m; reserved beds are "
                "exclusive sleep-action assignments persisted in the runtime overlay; room "
                "admissions are explicit capacity assignments"
            )
    from sheltersim.domain.environment import zone_environment_view

    for zone in frame["zones"]:
        conditions = zone_environment_view(world, zone["id"])
        zone["conditions"] = {
            key: getattr(conditions, key)
            for key in (
                "ventilation_effectiveness",
                "lighting_level",
                "privacy_level",
                "temperature_c",
                "condition_modifier_event_ids",
                "condition_assumption_ids",
            )
        }

    from sheltersim.contracts.runtime import entity_projections

    projections = entity_projections(world)
    context_people = population_context_people(world)
    for person in frame["people"]:
        if person["id"] in context_people:
            person["context"] = context_people[person["id"]]
        person_context = person.get("context")
        if not isinstance(person_context, dict):
            person_context = {}
        person["household_id"] = person_context.get("household_id", person["group_id"])
        if person["id"] in frame["presence"].get("people", {}):
            person["household_id"] = frame["presence"]["people"][person["id"]]["household_id"]
    for kind, entities in (("agent", frame["people"]), ("staff", frame["staff"])):
        for entity in entities:
            if (kind, entity["id"]) in projections:
                entity["extensions"] = projections[(kind, entity["id"])]
    rendered = TimelineFrame.model_validate(frame).model_dump(mode="json")
    if not incoming_arrivals:
        rendered.pop("incoming_arrivals", None)
    return rendered


def social_event_records(world: WorldState) -> tuple[list[dict[str, Any]], bool]:
    social_event_types = {
        "information_contact_update",
        "social_interaction",
        "information_correction",
    }
    social_events = []
    outcomes, window_truncated = world.event_log.recent_matching(frozenset(social_event_types))
    for outcome in outcomes:
        event = outcome.event
        if event.event_type not in social_event_types or outcome.status.value != "applied":
            continue
        payload = event.payload
        actors = sorted(
            {
                value
                for key in (
                    "agent_id",
                    "participant_agent_id",
                    "sender_agent_id",
                    "recipient_agent_id",
                )
                if isinstance((value := payload.get(key)), str)
            }
        )
        projected = {
            "event_id": event.event_id,
            "minute": outcome.realized_minute,
            "event_type": event.event_type,
            "actors": actors,
            "status": outcome.status.value,
            "causation_id": event.causation_id,
        }
        for field in (
            "interaction_type",
            "action",
            "topic_id",
            "message_id",
            "belief",
            "contact_basis",
            "contact_minutes",
            "reason_short",
            "zone_id",
        ):
            value = payload.get(field)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                projected[field] = value
        social_events.append(projected)
    return social_events, window_truncated


def person_projection(
    world: WorldState, identity: str, *, social_events: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Bounded selected-person evidence; no whole-world hash or model evaluation."""
    from copy import deepcopy

    presence = world.model_state.get("population_presence_v1", {}).get("people", {}).get(identity)
    agent = world.agents.get(identity)
    if agent is None:
        return {"person": None, "position": None, "presence": deepcopy(presence)}
    traits = (
        world.model_state.get("population_dynamics_v1", {})
        .get("profile_traits", world.model_configs.get("population", {}).get("people", {}))
        .get(identity)
    )
    context = population_context_people(world).get(identity)
    active_care_requests = []
    care_state = world.model_state.get("shared_operations_v1", {}).get("support_requests", {})
    if isinstance(care_state, dict):
        for request_id, raw_request in sorted(care_state.items()):
            if not isinstance(raw_request, dict) or raw_request.get("agent_id") != identity:
                continue
            request = deepcopy(raw_request)
            runtime = world.support_requests.get(request_id)
            request["runtime_status"] = runtime.status.value if runtime is not None else None
            request["assigned_staff_id"] = (
                runtime.assigned_staff_id if runtime is not None else None
            )
            if runtime is not None:
                request["waited_minutes"] = support_waited_minutes(
                    world, request_id, minute=world.minute_index
                )
                request["wait_reason_codes"] = list(
                    support_wait_reason_codes(world, request_id, minute=world.minute_index)
                )
            active_care_requests.append(request)
    person = {
        "id": identity,
        "group_id": agent.profile.group_id,
        "household_id": presence.get("household_id")
        if presence
        else (context or {}).get("household_id", agent.profile.group_id),
        "zone_id": agent.zone_id,
        "activity": agent.activity.value,
        "target_id": agent.target_id,
        "stress": agent.stress,
        "trust": agent.trust,
        "needs": vars(agent.needs).copy(),
        "mobility": agent.profile.mobility.value,
        "support_need": agent.profile.support_need,
        "traits": deepcopy(traits),
        "context": deepcopy(context),
        "decision": deepcopy(
            world.model_state.get("agent_dynamics_v1", {}).get("agents", {}).get(identity)
        ),
        "beliefs": deepcopy(
            dict(
                list(
                    world.model_state.get("information_spread_v1", {})
                    .get("beliefs", {})
                    .get(identity, {})
                    .items()
                )[:16]
            )
        ),
        "body": deepcopy(
            world.model_state.get("body_state_v1", {}).get("agents", {}).get(identity)
        ),
        "care_requests": active_care_requests,
        "care_history": care_history_for_agent(world, identity),
        "dialogue": None,
        "dialogue_status": "not_recorded",
    }
    overlay = world.model_state.get("geometry_overlay_v1", {})
    reservation = (
        overlay.get("bed_reservations", {}).get(identity) if isinstance(overlay, dict) else None
    )
    person["sleeping_place"] = deepcopy(reservation)
    admission = (
        overlay.get("room_admissions", {}).get(identity) if isinstance(overlay, dict) else None
    )
    person["physical_room"] = deepcopy(admission)
    if social_events is None:
        social_events, _ = social_event_records(world)
    dialogue = [
        item
        for item in social_events
        if identity in item["actors"]
        or item["event_type"] == "information_correction"
        and item.get("zone_id") == agent.zone_id
    ][-20:]
    person["dialogue"] = deepcopy(dialogue)
    person["dialogue_status"] = "structured_events_only" if dialogue else "no_retained_events"
    person["family_separated"] = (
        any(
            current_household_id(world, other.id) == current_household_id(world, agent.id)
            and other.zone_id != agent.zone_id
            for other in world.agents.values()
        )
        if current_household_id(world, agent.id) is not None
        else False
    )
    record = world.model_state.get("movement_v1", {}).get("positions", {}).get(identity, {})
    point = Point2D.model_validate(record["point"]) if record.get("point") is not None else None
    kind = record.get("position_kind", "unavailable")
    zone = world.zones[agent.zone_id]
    if (
        point is None
        and not world.model_configs.get("facility_geometry")
        and zone.display_x is not None
        and zone.display_y is not None
    ):
        point, kind = Point2D(x=zone.display_x, y=zone.display_y), "display_only"
    position = AgentPosition(
        agent_id=identity,
        zone_id=record.get("zone_id", agent.zone_id),
        point=point,
        edge_id=record.get("edge_id"),
        progress=record.get("progress", 0),
        position_kind=kind,
    ).model_dump(mode="json")
    return {"person": person, "position": position, "presence": deepcopy(presence)}
