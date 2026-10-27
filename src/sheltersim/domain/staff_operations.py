"""Autonomous, routed staff assignments with bounded causal task histories."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from sheltersim.contracts.operations import SHARED_OPERATIONS_KEY
from sheltersim.core.world import WorldState
from sheltersim.domain.models import ActiveTask
from sheltersim.domain.movement import shared_passage_edge_loads
from sheltersim.domain.operational import staff_is_available
from sheltersim.domain.staff_dynamics import (
    staff_dynamics_is_available,
    staff_dynamics_on_shift,
)
from sheltersim.events.models import (
    EventKind,
    EventLogRecord,
    EventStatus,
    EventSuppressed,
    ScheduledEvent,
)
from sheltersim.policies.space import SpaceRouter

STAFF_OPERATIONS_STATE_KEY = "staff_operations_v1"
STAFF_OPERATIONS_POLICY_VERSION = "autonomous-routed-staff-v1"
STAFF_OPERATIONS_SOURCE_ID = "ASM-STAFF-OPERATIONS-V1"
MAX_STAFF_HISTORY = 20_000
STAFF_RELIEF_POLICY_VERSION = "same-site-staff-relief-v1"
STAFF_RELIEF_SOURCE_ID = "ASM-STAFF-RELIEF-V1"

_DURATION_DEFAULTS = {
    "cleaning": 15,
    "maintenance": 20,
    "information_outreach": 10,
    "intake": 5,
    "patrol": 15,
}
_TASK_SKILLS = {
    "cleaning": {"cleaning", "sanitation"},
    "maintenance": {"maintenance", "repair"},
    "information_outreach": {"support", "information"},
    "intake": {"support", "intake"},
    "patrol": {"support", "patrol"},
}
_FIELDS = {
    "schema_version",
    "policy_version",
    "source_id",
    "config",
    "zones",
    "staff",
    "history",
}


def canonical_staff_operations_config(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - {
        "schema_version",
        "policy_version",
        "source_id",
        "task_duration_minutes",
        "sanitation_use_per_person_minute",
        "cleaning_sanitation_recovery",
        "outreach_uncertainty_relief",
        "patrol_reassurance",
        "thresholds_by_zone",
        "effects_by_zone",
    }:
        raise ValueError("staff_operations config has unsupported fields")
    config = {
        "schema_version": 1,
        "policy_version": STAFF_OPERATIONS_POLICY_VERSION,
        "source_id": STAFF_OPERATIONS_SOURCE_ID,
        "task_duration_minutes": dict(_DURATION_DEFAULTS),
        "sanitation_use_per_person_minute": 0.000005,
        "cleaning_sanitation_recovery": 0.15,
        "outreach_uncertainty_relief": 0.04,
        "patrol_reassurance": 0.01,
        "thresholds_by_zone": {},
        "effects_by_zone": {},
        **raw,
    }
    if (
        config["schema_version"] != 1
        or config["policy_version"] != STAFF_OPERATIONS_POLICY_VERSION
        or config["source_id"] != STAFF_OPERATIONS_SOURCE_ID
    ):
        raise ValueError("unsupported staff_operations policy or source")
    durations = config["task_duration_minutes"]
    if not isinstance(durations, dict) or set(durations) != set(_DURATION_DEFAULTS):
        raise ValueError("staff_operations task durations must define every registered task")
    for kind, value in durations.items():
        if type(value) is not int or not 1 <= value <= 24 * 60:
            raise ValueError(f"staff_operations duration for {kind} is outside 1..1440 minutes")
    for name in (
        "sanitation_use_per_person_minute",
        "cleaning_sanitation_recovery",
        "outreach_uncertainty_relief",
        "patrol_reassurance",
    ):
        value = config[name]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not 0.0 <= value <= 1.0
        ):
            raise ValueError(f"staff_operations {name} must be finite and within 0..1")
    thresholds_by_zone = config["thresholds_by_zone"]
    if not isinstance(thresholds_by_zone, dict):
        raise ValueError("staff_operations thresholds_by_zone must be an object")
    allowed_thresholds = {
        "cleaning_sanitation_below",
        "outreach_uncertainty_at_least",
        "patrol_stress_at_least",
    }
    for zone_id, thresholds in thresholds_by_zone.items():
        if (
            not isinstance(zone_id, str)
            or not zone_id
            or not isinstance(thresholds, dict)
            or not thresholds
            or set(thresholds) - allowed_thresholds
        ):
            raise ValueError("staff_operations per-zone threshold entry is invalid")
        for name, value in thresholds.items():
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"staff_operations per-zone {name} must be finite and within 0..1")
    effects_by_zone = config["effects_by_zone"]
    allowed_effects = {
        "sanitation_use_per_person_minute": (0.0, 0.01),
        "cleaning_sanitation_recovery": (0.0, 1.0),
        "outreach_uncertainty_relief": (0.0, 1.0),
        "patrol_reassurance": (0.0, 1.0),
    }
    if not isinstance(effects_by_zone, dict):
        raise ValueError("staff_operations effects_by_zone must be an object")
    for zone_id, effects in effects_by_zone.items():
        if (
            not isinstance(zone_id, str)
            or not zone_id
            or not isinstance(effects, dict)
            or not effects
            or set(effects) - set(allowed_effects)
        ):
            raise ValueError("staff_operations per-zone effect entry is invalid")
        for name, value in effects.items():
            lower, upper = allowed_effects[name]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not lower <= value <= upper
            ):
                raise ValueError(
                    f"staff_operations per-zone {name} must be finite and within "
                    f"{lower:g}..{upper:g}"
                )
    return config


def create_staff_operations_state(world: WorldState) -> dict[str, Any]:
    """Initialize team workload and a sanitation index without changing staff records."""
    config = canonical_staff_operations_config(world.model_configs.get("staff_operations"))
    return {
        "schema_version": 1,
        "policy_version": STAFF_OPERATIONS_POLICY_VERSION,
        "source_id": STAFF_OPERATIONS_SOURCE_ID,
        "config": config,
        "zones": {
            zone_id: {"sanitation": 1.0, "updated_minute": world.minute_index}
            for zone_id in sorted(world.zones)
        },
        "staff": {staff_id: _idle_record() for staff_id in sorted(world.staff)},
        "history": [],
    }


def validate_staff_operations_state(world: WorldState) -> None:
    state = world.model_state.get(STAFF_OPERATIONS_STATE_KEY)
    if state is None:
        return
    if (
        not isinstance(state, dict)
        or set(state) != _FIELDS
        or state.get("schema_version") != 1
        or state.get("policy_version") != STAFF_OPERATIONS_POLICY_VERSION
        or state.get("source_id") != STAFF_OPERATIONS_SOURCE_ID
        or not isinstance(state.get("zones"), dict)
        or set(state["zones"]) != set(world.zones)
        or not isinstance(state.get("staff"), dict)
        or set(state["staff"]) != set(world.staff)
        or not isinstance(state.get("history"), list)
        or len(state["history"]) > MAX_STAFF_HISTORY
    ):
        raise ValueError("staff_operations_v1 has an invalid exact schema")
    config = canonical_staff_operations_config(state["config"])
    if config != state["config"]:
        legacy_config = dict(config)
        for additive_field in ("thresholds_by_zone", "effects_by_zone"):
            if additive_field not in state["config"] and not config[additive_field]:
                legacy_config.pop(additive_field, None)
        if legacy_config != state["config"]:
            raise ValueError("staff_operations_v1 config is not canonical")
    if set(config["thresholds_by_zone"]) - set(world.zones):
        raise ValueError("staff_operations_v1 per-zone threshold references an unknown zone")
    if set(config["effects_by_zone"]) - set(world.zones):
        raise ValueError("staff_operations_v1 per-zone effect references an unknown zone")
    for zone_id, zone in state["zones"].items():
        if (
            not isinstance(zone, dict)
            or set(zone) != {"sanitation", "updated_minute"}
            or not isinstance(zone["sanitation"], (int, float))
            or isinstance(zone["sanitation"], bool)
            or not math.isfinite(zone["sanitation"])
            or not 0.0 <= zone["sanitation"] <= 1.0
            or type(zone["updated_minute"]) is not int
            or zone["updated_minute"] < 0
        ):
            raise ValueError(f"staff_operations_v1 zone state for {zone_id!r} is invalid")
    fields = {
        "phase",
        "task_id",
        "task_kind",
        "target_id",
        "target_zone_id",
        "reason",
        "started_minute",
        "travel_due_minute",
        "arrival_minute",
        "due_minute",
        "route_zone_ids",
        "route_edge_ids",
        "travel_minutes",
        "causation_event_ids",
    }
    pending_events = world.event_queue.pending()
    for staff_id, record in state["staff"].items():
        if (
            not isinstance(record, dict)
            or set(record) != fields
            or record["phase"] not in {"idle", "travel", "working"}
        ):
            raise ValueError(f"staff_operations_v1 record for {staff_id!r} is invalid")
        if record["phase"] == "idle":
            if record != _idle_record():
                raise ValueError(f"idle staff_operations_v1 record for {staff_id!r} retains a task")
            continue
        if (
            record["task_id"] not in world.active_tasks
            or world.active_tasks[record["task_id"]].staff_id != staff_id
            or world.staff[staff_id].active_task_id != record["task_id"]
            or world.staff[staff_id].work_queue != [record["task_id"]]
            or world.staff[staff_id].busy_until != record["due_minute"]
            or record["task_kind"] not in _DURATION_DEFAULTS
            or record["target_zone_id"] not in world.zones
            or type(record["started_minute"]) is not int
            or type(record["due_minute"]) is not int
            or record["due_minute"] <= record["started_minute"]
            or not isinstance(record["route_zone_ids"], list)
            or not isinstance(record["route_edge_ids"], list)
            or any(zone_id not in world.zones for zone_id in record["route_zone_ids"])
            or any(edge_id not in world.zone_edges for edge_id in record["route_edge_ids"])
            or type(record["travel_minutes"]) is not int
            or record["travel_minutes"] < 0
            or not isinstance(record["causation_event_ids"], list)
            or type(record["started_minute"]) is not int
            or record["started_minute"] < 0
            or not isinstance(record["reason"], str)
            or not record["reason"]
            or (record["target_id"] is not None and record["target_id"] not in world.facilities)
        ):
            raise ValueError(f"active staff_operations_v1 record for {staff_id!r} is invalid")
        route_is_valid = (
            len(record["route_zone_ids"]) == len(record["route_edge_ids"]) + 1
            and bool(record["route_edge_ids"])
            and all(
                world.zone_edges[edge_id].from_zone_id == record["route_zone_ids"][index]
                and world.zone_edges[edge_id].to_zone_id == record["route_zone_ids"][index + 1]
                for index, edge_id in enumerate(record["route_edge_ids"])
            )
            and record["route_zone_ids"][0] == world.staff[staff_id].zone_id
            and record["route_zone_ids"][-1] == record["target_zone_id"]
            and record["travel_minutes"]
            == sum(world.zone_edges[edge_id].travel_minutes for edge_id in record["route_edge_ids"])
        )
        if record["phase"] == "travel":
            if (
                not route_is_valid
                or type(record["travel_due_minute"]) is not int
                or record["travel_due_minute"]
                != record["started_minute"] + record["travel_minutes"]
                or record["arrival_minute"] is not None
            ):
                raise ValueError(f"staff_operations_v1 travel record for {staff_id!r} is invalid")
        elif (
            record["route_zone_ids"]
            or record["route_edge_ids"]
            or record["travel_due_minute"] is not None
            or type(record["arrival_minute"]) is not int
            or record["arrival_minute"] != record["started_minute"] + record["travel_minutes"]
            or world.staff[staff_id].zone_id != record["target_zone_id"]
        ):
            raise ValueError(f"staff_operations_v1 working record for {staff_id!r} is invalid")
        expected_due = (
            record["started_minute"]
            + record["travel_minutes"]
            + config["task_duration_minutes"][record["task_kind"]]
        )
        if record["due_minute"] != expected_due:
            raise ValueError(f"staff_operations_v1 due minute for {staff_id!r} is invalid")
        completions = [
            event
            for event in pending_events
            if event.event_type == "staff_operation_complete"
            and event.payload.get("task_id") == record["task_id"]
            and event.payload.get("staff_id") == staff_id
            and event.due_minute == record["due_minute"]
        ]
        arrivals = [
            event
            for event in pending_events
            if event.event_type == "staff_operation_arrive"
            and event.payload.get("task_id") == record["task_id"]
            and event.payload.get("staff_id") == staff_id
        ]
        if (
            len(completions) != 1
            or completions[0].payload.get("task_kind") != record["task_kind"]
            or completions[0].payload.get("target_id") != record["target_id"]
            or len(arrivals) != int(record["phase"] == "travel")
            or (arrivals and arrivals[0].due_minute != record["travel_due_minute"])
            or (arrivals and arrivals[0].payload.get("target_zone_id") != record["target_zone_id"])
        ):
            raise ValueError(
                f"staff_operations_v1 event references for {staff_id!r} are inconsistent"
            )
    for item in state["history"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "task_id",
                "staff_id",
                "task_kind",
                "target_id",
                "target_zone_id",
                "reason",
                "status",
                "start_minute",
                "arrival_minute",
                "complete_minute",
                "travel_minutes",
                "causation_event_ids",
            }
            or item["staff_id"] not in world.staff
            or item["task_kind"] not in _DURATION_DEFAULTS
            or item["status"] not in {"completed", "blocked", "interrupted"}
            or item["target_zone_id"] not in world.zones
            or (item["target_id"] is not None and item["target_id"] not in world.facilities)
            or not isinstance(item["task_id"], str)
            or not isinstance(item["reason"], str)
            or any(
                type(item[name]) is not int or item[name] < 0
                for name in ("start_minute", "complete_minute", "travel_minutes")
            )
            or item["complete_minute"] < item["start_minute"]
            or (
                item["arrival_minute"] is None
                and not (item["status"] == "interrupted" and item["travel_minutes"] > 0)
            )
            or (
                item["arrival_minute"] is not None
                and (
                    type(item["arrival_minute"]) is not int
                    or not item["start_minute"] <= item["arrival_minute"] <= item["complete_minute"]
                )
            )
            or not isinstance(item["causation_event_ids"], list)
            or any(
                not isinstance(event_id, str) or not event_id
                for event_id in item["causation_event_ids"]
            )
        ):
            raise ValueError("staff_operations_v1 history item is invalid")


def add_staff_operations_agent(world: WorldState, staff_id: str) -> None:
    state = world.model_state.get(STAFF_OPERATIONS_STATE_KEY)
    if state is not None and staff_id in world.staff:
        state["staff"][staff_id] = _idle_record()


def update_staff_operations(world: WorldState, minute: int) -> None:
    """Apply occupancy-linked sanitation demand using bounded per-person rates."""
    state = world.model_state.get(STAFF_OPERATIONS_STATE_KEY)
    if state is None:
        return
    for zone_id in sorted(world.zones):
        load = sum(agent.zone_id == zone_id for agent in world.agents.values())
        zone = state["zones"][zone_id]
        elapsed = max(0, minute - zone["updated_minute"])
        rate = (
            state["config"]["effects_by_zone"]
            .get(zone_id, {})
            .get(
                "sanitation_use_per_person_minute",
                state["config"]["sanitation_use_per_person_minute"],
            )
        )
        zone["sanitation"] = max(0.0, zone["sanitation"] - load * rate * elapsed)
        zone["updated_minute"] = minute


def assign_staff_operations(world: WorldState, minute: int, router: SpaceRouter) -> tuple[str, ...]:
    """Choose the highest-need available task by role, urgency and reachable travel time."""
    state = world.model_state.get(STAFF_OPERATIONS_STATE_KEY)
    if state is None:
        return ()
    from sheltersim.policies.registry import create_staff_operations_formula

    formula = create_staff_operations_formula(world.model_configs)
    formula_selected = "staff_operations" in world.model_configs.get("policy_registry", {})
    formula_source_id = formula.source_id if formula_selected else STAFF_OPERATIONS_SOURCE_ID
    formula_policy_id = formula.model_id if formula_selected else STAFF_OPERATIONS_POLICY_VERSION
    event_source_id = formula_source_id if formula_selected else STAFF_OPERATIONS_POLICY_VERSION
    assigned: list[str] = []
    # Rotate eligible staff by recent and cumulative workload so stable IDs do
    # not always win same-minute ties. Only tasks the staff member can perform
    # enter their candidate set below.
    recent_cutoff = max(0, minute - 240)
    workload: dict[str, tuple[int, int]] = {
        staff_id: (
            sum(
                item["staff_id"] == staff_id and item["complete_minute"] >= recent_cutoff
                for item in state["history"]
            ),
            sum(item["staff_id"] == staff_id for item in state["history"]),
        )
        for staff_id in world.staff
    }
    staff_dynamics = world.model_state.get("staff_dynamics_v1", {}).get("staff", {})
    passage_loads = shared_passage_edge_loads(world, minute)
    body = world.model_state.get("body_state_v1", {}).get("agents", {})
    reserved_operations = {
        (record["task_kind"], record["target_zone_id"], record["target_id"])
        for record in state["staff"].values()
        if record["phase"] != "idle"
    }
    for staff_id, staff in sorted(
        world.staff.items(),
        key=lambda item: (
            *workload[item[0]],
            round(1000 * staff_dynamics.get(item[0], {}).get("fatigue", 0.0)),
            item[0],
        ),
    ):
        if (
            staff.active_task_id is not None
            or state["staff"][staff_id]["phase"] != "idle"
            or not staff_is_available(world, staff_id)
            or not staff_dynamics_is_available(world, staff_id, minute)
        ):
            continue
        candidates: list[tuple[float, str, str, str | None, str]] = []
        skills = set(staff.skills)
        zone_thresholds = state["config"].get("thresholds_by_zone", {})
        if skills & {"cleaning", "sanitation"}:
            for zone_id, record in sorted(state["zones"].items()):
                cleaning_threshold = zone_thresholds.get(zone_id, {}).get(
                    "cleaning_sanitation_below", 0.85
                )
                if (
                    record["sanitation"] < cleaning_threshold
                    and ("cleaning", zone_id, None) not in reserved_operations
                ):
                    candidates.append(
                        (
                            1.0 - record["sanitation"],
                            "cleaning",
                            zone_id,
                            None,
                            f"sanitation_index_below_{cleaning_threshold:g}",
                        )
                    )
        if skills & {"maintenance", "repair"}:
            for facility in sorted(world.facilities.values(), key=lambda item: item.id):
                deficit = facility.capacity - facility.working_units
                if (
                    deficit > 0
                    and not _maintenance_blocked(world, facility.id)
                    and ("maintenance", facility.zone_id, facility.id) not in reserved_operations
                ):
                    candidates.append(
                        (
                            float(deficit),
                            "maintenance",
                            facility.zone_id,
                            facility.id,
                            "facility_working_units_below_capacity",
                        )
                    )
        if skills & {"support", "information"} and body:
            by_zone: dict[str, list[float]] = {}
            for agent_id, agent in world.agents.items():
                record = body.get(agent_id)
                if record is not None:
                    by_zone.setdefault(agent.zone_id, []).append(record["uncertainty"])
            for zone_id, values in sorted(by_zone.items()):
                mean = math.fsum(values) / len(values)
                outreach_threshold = zone_thresholds.get(zone_id, {}).get(
                    "outreach_uncertainty_at_least", 0.45
                )
                if (
                    mean >= outreach_threshold
                    and ("information_outreach", zone_id, None) not in reserved_operations
                ):
                    candidates.append(
                        (
                            mean,
                            "information_outreach",
                            zone_id,
                            None,
                            f"mean_information_uncertainty_at_least_{outreach_threshold:g}",
                        )
                    )
        if skills & {"support", "patrol"}:
            for zone_id in sorted(world.zones):
                occupants = [agent for agent in world.agents.values() if agent.zone_id == zone_id]
                if occupants:
                    mean_stress = math.fsum(agent.stress for agent in occupants) / len(occupants)
                    patrol_threshold = zone_thresholds.get(zone_id, {}).get(
                        "patrol_stress_at_least", 0.6
                    )
                    if (
                        mean_stress >= patrol_threshold
                        and ("patrol", zone_id, None) not in reserved_operations
                    ):
                        candidates.append(
                            (
                                mean_stress * 0.5,
                                "patrol",
                                zone_id,
                                None,
                                f"mean_zone_stress_at_least_{patrol_threshold:g}",
                            )
                        )
        ranked: list[
            tuple[
                tuple[float, float, str, str, str],
                tuple[float, str, str, str | None, str, Any],
            ]
        ] = []
        for urgency, kind, zone_id, target_id, reason in candidates:
            route = router.route(staff.zone_id, zone_id, staff_id, edge_loads=passage_loads)
            if route is not None:
                ranked.append(
                    (
                        formula.rank_key(urgency, route.travel_minutes, kind, zone_id, target_id),
                        (urgency, kind, zone_id, target_id, reason, route),
                    )
                )
        if not ranked:
            continue
        _, (_, task_kind, target_zone, target_id, reason, route) = max(
            ranked, key=lambda item: item[0]
        )
        travel_minutes = route.travel_minutes
        work_minutes = state["config"]["task_duration_minutes"][task_kind]
        due = minute + travel_minutes + work_minutes
        if due >= world.duration_minutes:
            continue
        sequence = world.allocate_event_sequence()
        task_id = f"staff-operation-{sequence}"
        arrival_minute = minute + travel_minutes
        record = {
            "phase": "travel" if travel_minutes else "working",
            "task_id": task_id,
            "task_kind": task_kind,
            "target_id": target_id,
            "target_zone_id": target_zone,
            "reason": reason,
            "started_minute": minute,
            "travel_due_minute": arrival_minute if travel_minutes else None,
            "arrival_minute": arrival_minute if not travel_minutes else None,
            "due_minute": due,
            "route_zone_ids": list(route.zone_ids) if travel_minutes else [],
            "route_edge_ids": list(route.edge_ids) if travel_minutes else [],
            "travel_minutes": travel_minutes,
            "causation_event_ids": [],
        }
        state["staff"][staff_id] = record
        for edge_id in route.edge_ids:
            passage_loads[edge_id] = passage_loads.get(edge_id, 0) + 1
        world.active_tasks[task_id] = ActiveTask(
            id=task_id,
            kind="staff_operation",
            agent_id=None,
            resource_id=None,
            start_minute=minute,
            due_minute=due,
            staff_id=staff_id,
        )
        staff.active_task_id = task_id
        staff.work_queue[:] = [task_id]
        staff.busy_until = due
        if travel_minutes:
            world.schedule_event(
                ScheduledEvent(
                    event_id=f"staff-operation-arrive-{sequence}",
                    kind=EventKind.COMPLETION,
                    due_minute=arrival_minute,
                    priority=5,
                    sequence=sequence,
                    event_type="staff_operation_arrive",
                    payload={
                        "staff_id": staff_id,
                        "task_id": task_id,
                        "target_zone_id": target_zone,
                    },
                    source_id=event_source_id,
                    provenance={
                        "source_type": "assumption",
                        "source_id": formula_source_id,
                        "policy_version": formula_policy_id,
                    },
                )
            )
        completion_sequence = world.allocate_event_sequence()
        world.schedule_event(
            ScheduledEvent(
                event_id=f"staff-operation-complete-{completion_sequence}",
                kind=EventKind.COMPLETION,
                due_minute=due,
                priority=5,
                sequence=completion_sequence,
                event_type="staff_operation_complete",
                payload={
                    "staff_id": staff_id,
                    "task_id": task_id,
                    "task_kind": task_kind,
                    "target_id": target_id,
                    "target_zone_id": target_zone,
                },
                source_id=event_source_id,
                provenance={
                    "source_type": "assumption",
                    "source_id": formula_source_id,
                    "policy_version": formula_policy_id,
                    "reason": reason,
                },
            )
        )
        reserved_operations.add((task_kind, target_zone, target_id))
        assigned.append(task_id)
    if assigned and "movement_v1" in world.model_state:
        from sheltersim.domain.movement import staff_position_snapshots

        staff_position_snapshots(world, boundary_minute=minute + 1)
    return tuple(assigned)


_TASK_NAMES = {
    "cleaning": "清掃",
    "maintenance": "保守",
    "information_outreach": "情報提供",
    "intake": "受付",
    "patrol": "巡回",
}


def _missing_task_skill_message(world: WorldState, staff_id: str, task_kind: str) -> str:
    """Tell the operator, before approval, who could take an order this worker cannot (#75).

    Generated Scenarios staff no one for cleaning or maintenance: in a shelter that
    work falls to evacuee rotas, the facility manager or contractors, not to staff.
    """
    skills = _TASK_SKILLS[task_kind]
    name = _TASK_NAMES[task_kind]
    skill_list = "・".join(sorted(skills))
    qualified = sorted(
        other_id for other_id, other in world.staff.items() if skills.intersection(other.skills)
    )
    if qualified:
        return (
            f"staff_lacks_task_skill: {staff_id}は{name}の技能（{skill_list}）を持ちません。"
            f"技能を持つ職員（{'、'.join(qualified)}）に指示してください。"
        )
    advice = "担当変更（set_staff_role）で職員に技能を与えてから指示してください。"
    if task_kind == "maintenance":
        advice = (
            "設備の修理は修理の手配（facility_repair）で指示するか、"
            "担当変更（set_staff_role）で職員に技能を与えてから指示してください。"
        )
    return f"no_qualified_staff: {name}の技能（{skill_list}）を持つ職員がいません。{advice}"


def validate_staff_operation_order(
    world: WorldState,
    *,
    staff_id: str,
    task_kind: str,
    target_zone_id: str,
    target_id: str | None,
    minute: int,
) -> Any:
    """Validate an operator-ordered task and return its reachable staff route."""
    from sheltersim.domain.movement import route_is_physically_clear
    from sheltersim.domain.zones import ZoneGraph
    from sheltersim.events.models import EventApplicationError
    from sheltersim.policies.space import SpaceRouter

    state = world.model_state.get(STAFF_OPERATIONS_STATE_KEY)
    staff = world.staff.get(staff_id)
    record = state.get("staff", {}).get(staff_id) if isinstance(state, dict) else None
    if state is None or SHARED_OPERATIONS_KEY not in world.model_state:
        raise EventApplicationError("staff task order requires staff and shared operation state")
    if staff is None or not isinstance(record, dict):
        raise EventApplicationError("staff task order references unknown staff")
    if (
        record.get("phase") != "idle"
        or staff.active_task_id is not None
        or not staff_is_available(world, staff_id)
        or not staff_dynamics_is_available(world, staff_id, minute)
    ):
        raise EventApplicationError("staff task order requires an idle, available, on-shift worker")
    if task_kind not in _TASK_SKILLS:
        raise EventApplicationError("staff task order uses an unregistered task kind")
    if not _TASK_SKILLS[task_kind].intersection(staff.skills):
        raise EventApplicationError(_missing_task_skill_message(world, staff_id, task_kind))
    if target_zone_id not in world.zones:
        raise EventApplicationError("staff task order references an unknown target zone")
    if task_kind == "maintenance":
        facility = world.facilities.get(target_id or "")
        if facility is None or facility.zone_id != target_zone_id:
            raise EventApplicationError(
                "maintenance order must target a facility in its selected zone"
            )
        if facility.working_units >= facility.capacity or _maintenance_blocked(world, facility.id):
            raise EventApplicationError(
                "maintenance order has no currently repairable facility unit"
            )
    elif target_id is not None:
        raise EventApplicationError("only maintenance orders accept a target facility")
    if task_kind in {"information_outreach", "patrol"}:
        if not any(agent.zone_id == target_zone_id for agent in world.agents.values()):
            raise EventApplicationError("staff support order requires residents in its target zone")
        if task_kind == "information_outreach" and not world.model_state.get(
            "body_state_v1", {}
        ).get("agents"):
            raise EventApplicationError("information outreach requires the body-state policy")
    if task_kind == "intake":
        population = world.model_state.get("population_dynamics_v1")
        if not isinstance(population, dict) or not population.get("pending_arrivals"):
            raise EventApplicationError("intake order requires pending arrivals")
    graph = ZoneGraph.from_states(world.zones, world.zone_edges)
    if "facility_geometry" in world.model_configs:
        graph = ZoneGraph(
            zones=graph.zones,
            edges=tuple(
                edge
                for edge in graph.edges
                if route_is_physically_clear(world, edge.id, mobility="independent")
            ),
        )
    router = SpaceRouter(graph)
    route = router.route(
        staff.zone_id,
        target_zone_id,
        staff_id,
        edge_loads=shared_passage_edge_loads(world, minute),
    )
    if route is None:
        raise EventApplicationError("staff task order has no reachable route")
    duration = state["config"]["task_duration_minutes"][task_kind]
    if minute + route.travel_minutes + duration >= world.duration_minutes:
        raise EventApplicationError("staff task order would finish beyond the Run boundary")
    return route


def order_staff_operation(
    world: WorldState,
    *,
    staff_id: str,
    task_kind: str,
    target_zone_id: str,
    target_id: str | None,
    reason: str,
    causation_event_id: str,
    source_id: str | None,
) -> str:
    """Start one explicit typed staff task using the normal routed task lifecycle."""
    from sheltersim.events.models import EventApplicationError, EventKind, ScheduledEvent
    from sheltersim.policies.registry import create_staff_operations_formula

    route = validate_staff_operation_order(
        world,
        staff_id=staff_id,
        task_kind=task_kind,
        target_zone_id=target_zone_id,
        target_id=target_id,
        minute=world.minute_index,
    )
    state = world.model_state[STAFF_OPERATIONS_STATE_KEY]
    duration = state["config"]["task_duration_minutes"][task_kind]
    due_minute = world.minute_index + route.travel_minutes + duration
    if due_minute >= world.duration_minutes:
        raise EventApplicationError("staff task order would finish beyond the Run boundary")
    formula = create_staff_operations_formula(world.model_configs)
    selected = "staff_operations" in world.model_configs.get("policy_registry", {})
    formula_source_id = formula.source_id if selected else STAFF_OPERATIONS_SOURCE_ID
    formula_policy_id = formula.model_id if selected else STAFF_OPERATIONS_POLICY_VERSION
    operation_source_id = source_id or formula_source_id
    sequence = world.allocate_event_sequence()
    task_id = f"staff-operation-{sequence}"
    arrival_minute = world.minute_index + route.travel_minutes
    state["staff"][staff_id] = {
        "phase": "travel" if route.travel_minutes else "working",
        "task_id": task_id,
        "task_kind": task_kind,
        "target_id": target_id,
        "target_zone_id": target_zone_id,
        "reason": reason,
        "started_minute": world.minute_index,
        "travel_due_minute": arrival_minute if route.travel_minutes else None,
        "arrival_minute": None if route.travel_minutes else arrival_minute,
        "due_minute": due_minute,
        "route_zone_ids": list(route.zone_ids) if route.travel_minutes else [],
        "route_edge_ids": list(route.edge_ids) if route.travel_minutes else [],
        "travel_minutes": route.travel_minutes,
        "causation_event_ids": [causation_event_id],
    }
    world.active_tasks[task_id] = ActiveTask(
        id=task_id,
        kind="staff_operation",
        agent_id=None,
        resource_id=None,
        start_minute=world.minute_index,
        due_minute=due_minute,
        staff_id=staff_id,
    )
    staff = world.staff[staff_id]
    staff.active_task_id = task_id
    staff.work_queue[:] = [task_id]
    staff.busy_until = due_minute
    provenance = {
        "source_type": "assumption",
        "source_id": formula_source_id,
        "policy_version": formula_policy_id,
        "reason": reason,
    }
    if route.travel_minutes:
        world.schedule_event(
            ScheduledEvent(
                event_id=f"staff-operation-arrive-{sequence}",
                kind=EventKind.COMPLETION,
                due_minute=arrival_minute,
                priority=5,
                sequence=sequence,
                event_type="staff_operation_arrive",
                payload={
                    "staff_id": staff_id,
                    "task_id": task_id,
                    "target_zone_id": target_zone_id,
                },
                source_id=operation_source_id,
                causation_id=causation_event_id,
                provenance=provenance,
            )
        )
    completion_sequence = world.allocate_event_sequence()
    world.schedule_event(
        ScheduledEvent(
            event_id=f"staff-operation-complete-{completion_sequence}",
            kind=EventKind.COMPLETION,
            due_minute=due_minute,
            priority=5,
            sequence=completion_sequence,
            event_type="staff_operation_complete",
            payload={
                "staff_id": staff_id,
                "task_id": task_id,
                "task_kind": task_kind,
                "target_id": target_id,
                "target_zone_id": target_zone_id,
            },
            source_id=operation_source_id,
            causation_id=causation_event_id,
            provenance=provenance,
        )
    )
    if "movement_v1" in world.model_state:
        from sheltersim.domain.movement import staff_position_snapshots

        staff_position_snapshots(world, boundary_minute=world.minute_index + 1)
    return task_id


def schedule_staff_operation_relief(world: WorldState, minute: int) -> tuple[str, ...]:
    """Schedule same-site relief before a working or traveling staff member goes off shift.

    A replacement already at the task destination can take over the same typed task.
    If the former worker was still traveling, the replacement starts the work interval
    from that destination; no partial task effect is inferred or retained.
    """
    state = world.model_state.get(STAFF_OPERATIONS_STATE_KEY)
    if not isinstance(state, dict) or minute + 1 >= world.duration_minutes:
        return ()
    handoffs: list[str] = []
    pending_handoffs = {
        event.payload.get("task_id")
        for event in world.event_queue.pending()
        if event.event_type == "staff_operation_relief"
    }
    skills_by_task = {
        "cleaning": {"cleaning", "sanitation"},
        "maintenance": {"maintenance", "repair"},
        "information_outreach": {"support", "information"},
        "intake": {"support", "intake"},
        "patrol": {"support", "patrol"},
    }
    recent_cutoff = max(0, minute - 240)
    workload = {
        staff_id: (
            sum(
                row["staff_id"] == staff_id and row["complete_minute"] >= recent_cutoff
                for row in state["history"]
            ),
            sum(row["staff_id"] == staff_id for row in state["history"]),
            round(
                1000
                * world.model_state.get("staff_dynamics_v1", {})
                .get("staff", {})
                .get(staff_id, {})
                .get("fatigue", 0.0)
            ),
            staff_id,
        )
        for staff_id in world.staff
    }
    for staff_id, operation in sorted(state["staff"].items()):
        if operation["phase"] == "idle" or operation["task_id"] in pending_handoffs:
            continue
        handoff_minute = minute + 1
        unavailable = not staff_is_available(world, staff_id) or not staff_dynamics_on_shift(
            world, staff_id, handoff_minute
        )
        if not unavailable:
            continue
        task = world.active_tasks.get(operation["task_id"])
        if task is None or task.staff_id != staff_id:
            continue
        if operation["due_minute"] <= handoff_minute:
            continue
        target_zone = operation["target_zone_id"]
        work_minutes = state["config"]["task_duration_minutes"][operation["task_kind"]]
        if handoff_minute + work_minutes >= world.duration_minutes:
            continue
        required = skills_by_task[operation["task_kind"]]
        replacements = [
            candidate
            for candidate in world.staff.values()
            if candidate.id != staff_id
            and candidate.zone_id == target_zone
            and required.intersection(candidate.skills)
            and candidate.active_task_id is None
            and state["staff"][candidate.id]["phase"] == "idle"
            and staff_is_available(world, candidate.id)
            and staff_dynamics_is_available(world, candidate.id, handoff_minute)
        ]
        if not replacements:
            continue
        replacement = min(replacements, key=lambda candidate: workload[candidate.id])
        sequence = world.allocate_event_sequence()
        event = ScheduledEvent(
            event_id=f"staff-operation-relief-{sequence}",
            kind=EventKind.ENDOGENOUS,
            due_minute=handoff_minute,
            priority=-5,
            sequence=sequence,
            event_type="staff_operation_relief",
            payload={
                "task_id": operation["task_id"],
                "previous_staff_id": staff_id,
                "replacement_staff_id": replacement.id,
                "reason_code": "shift_or_availability_boundary",
            },
            source_id=STAFF_RELIEF_POLICY_VERSION,
            provenance={
                "source_type": "assumption",
                "source_id": STAFF_RELIEF_SOURCE_ID,
                "policy_version": STAFF_RELIEF_POLICY_VERSION,
            },
        )
        world.schedule_event(event)
        handoffs.append(event.event_id)
    return tuple(handoffs)


def handoff_staff_operation(world: WorldState, event: ScheduledEvent) -> EventLogRecord | None:
    """Transfer a live task to a qualified co-located replacement and record the handoff."""
    from sheltersim.domain.care_receipts import make_staff_handoff_record
    from sheltersim.events.models import EventApplicationError
    from sheltersim.policies.registry import create_staff_operations_formula

    if (
        event.event_type != "staff_operation_relief"
        or event.kind is not EventKind.ENDOGENOUS
        or event.due_minute != world.minute_index
        or event.source_id != STAFF_RELIEF_POLICY_VERSION
        or event.provenance.get("source_type") != "assumption"
        or event.provenance.get("source_id") != STAFF_RELIEF_SOURCE_ID
        or event.provenance.get("policy_version") != STAFF_RELIEF_POLICY_VERSION
        or set(event.payload)
        != {"task_id", "previous_staff_id", "replacement_staff_id", "reason_code"}
    ):
        raise EventApplicationError("staff operation relief provenance or payload is invalid")
    task_id = event.payload["task_id"]
    previous_id = event.payload["previous_staff_id"]
    replacement_id = event.payload["replacement_staff_id"]
    state = world.model_state.get(STAFF_OPERATIONS_STATE_KEY)
    operation = state.get("staff", {}).get(previous_id) if isinstance(state, dict) else None
    task = world.active_tasks.get(task_id)
    previous = world.staff.get(previous_id)
    replacement = world.staff.get(replacement_id)
    if (
        not isinstance(operation, dict)
        or operation["phase"] == "idle"
        or operation["task_id"] != task_id
        or operation["target_zone_id"] != (replacement.zone_id if replacement else None)
        or task is None
        or task.staff_id != previous_id
        or previous is None
        or previous.active_task_id != task_id
        or replacement is None
        or replacement.active_task_id is not None
        or state["staff"].get(replacement_id, {}).get("phase") != "idle"
        or not staff_is_available(world, replacement_id)
        or not staff_dynamics_is_available(world, replacement_id, world.minute_index)
        or staff_dynamics_on_shift(world, previous_id, world.minute_index)
        and staff_is_available(world, previous_id)
        or operation["due_minute"] <= world.minute_index
    ):
        from sheltersim.events.models import EventSuppressed

        raise EventSuppressed("staff relief is stale or its replacement is no longer available")

    required = {
        "cleaning": {"cleaning", "sanitation"},
        "maintenance": {"maintenance", "repair"},
        "information_outreach": {"support", "information"},
        "intake": {"support", "intake"},
        "patrol": {"support", "patrol"},
    }[operation["task_kind"]]
    if not required.intersection(replacement.skills):
        raise EventSuppressed("replacement staff lacks the task skill")

    formula = create_staff_operations_formula(world.model_configs)
    formula_selected = "staff_operations" in world.model_configs.get("policy_registry", {})
    formula_source_id = formula.source_id if formula_selected else STAFF_OPERATIONS_SOURCE_ID
    formula_policy_id = formula.model_id if formula_selected else STAFF_OPERATIONS_POLICY_VERSION
    event_source_id = formula_source_id if formula_selected else STAFF_OPERATIONS_POLICY_VERSION

    pending = [
        candidate
        for candidate in world.event_queue.pending()
        if candidate.event_type in {"staff_operation_arrive", "staff_operation_complete"}
        and candidate.payload.get("task_id") == task_id
    ]
    if not any(candidate.event_type == "staff_operation_complete" for candidate in pending):
        raise EventApplicationError("relieved staff operation lacks a pending completion")
    previous_position = None
    if "movement_v1" in world.model_state:
        from sheltersim.domain.movement import staff_position_snapshots

        previous_position = staff_position_snapshots(world, boundary_minute=world.minute_index).get(
            previous_id
        )
        if previous_position is not None:
            previous.zone_id = previous_position["zone_id"]
    for candidate in pending:
        world.event_queue.remove(candidate.event_id)
        world.event_log.append(
            EventLogRecord(
                event=candidate,
                realized_minute=world.minute_index,
                status=EventStatus.SUPPRESSED,
                detail="staff operation was replaced by a qualified co-located worker",
            )
        )

    previous.active_task_id = None
    previous.work_queue.clear()
    previous.busy_until = 0
    replacement.active_task_id = task_id
    replacement.work_queue[:] = [task_id]
    replacement.busy_until = operation["due_minute"]
    task.staff_id = replacement_id
    updated = deepcopy(operation)
    updated["phase"] = "working"
    updated["started_minute"] = world.minute_index
    updated["travel_due_minute"] = None
    updated["arrival_minute"] = world.minute_index
    work_minutes = state["config"]["task_duration_minutes"][operation["task_kind"]]
    updated["due_minute"] = world.minute_index + work_minutes
    updated["route_zone_ids"] = []
    updated["route_edge_ids"] = []
    updated["travel_minutes"] = 0
    updated["causation_event_ids"] = sorted(set(updated["causation_event_ids"] + [event.event_id]))
    task.start_minute = world.minute_index
    task.due_minute = updated["due_minute"]
    replacement.busy_until = updated["due_minute"]
    state["staff"][previous_id] = _idle_record()
    state["staff"][replacement_id] = updated

    sequence = world.allocate_event_sequence()
    world.schedule_event(
        ScheduledEvent(
            event_id=f"staff-operation-complete-{sequence}",
            kind=EventKind.COMPLETION,
            due_minute=updated["due_minute"],
            priority=5,
            sequence=sequence,
            event_type="staff_operation_complete",
            payload={
                "staff_id": replacement_id,
                "task_id": task_id,
                "task_kind": updated["task_kind"],
                "target_id": updated["target_id"],
                "target_zone_id": updated["target_zone_id"],
            },
            source_id=event_source_id,
            causation_id=event.event_id,
            provenance={
                "source_type": "assumption",
                "source_id": formula_source_id,
                "policy_version": formula_policy_id,
                "reason": updated["reason"],
            },
        )
    )
    if "movement_v1" in world.model_state:
        from sheltersim.domain.movement import staff_position_snapshots

        staff_position_snapshots(world, boundary_minute=world.minute_index)
        if previous_position is not None:
            world.model_state["movement_v1"]["staff_positions"][previous_id] = previous_position
    handoff = make_staff_handoff_record(
        world,
        task_id=task_id,
        previous_staff_id=previous_id,
        replacement_staff_id=replacement_id,
        reason_code=event.payload["reason_code"],
        causation_id=event.event_id,
        source_ids=(STAFF_RELIEF_SOURCE_ID,),
    )
    return handoff[0] if handoff is not None else None


def arrive_staff_operation(world: WorldState, event: ScheduledEvent) -> None:
    state = world.model_state.get(STAFF_OPERATIONS_STATE_KEY)
    staff_id, task_id = event.payload.get("staff_id"), event.payload.get("task_id")
    record = (
        state["staff"].get(staff_id)
        if isinstance(state, dict) and isinstance(staff_id, str)
        else None
    )
    staff = world.staff.get(staff_id) if isinstance(staff_id, str) else None
    task = world.active_tasks.get(task_id) if isinstance(task_id, str) else None
    if (
        record is None
        or record["phase"] != "travel"
        or record["task_id"] != task_id
        or staff is None
        or task is None
        or staff.active_task_id != task_id
        or event.due_minute != record["travel_due_minute"]
        or event.payload.get("target_zone_id") != record["target_zone_id"]
    ):
        from sheltersim.events.models import EventApplicationError

        raise EventApplicationError("staff travel arrival does not match an active operation")
    if not staff_is_available(world, staff_id):
        interrupt_staff_operation(world, staff_id, event, current_event_id=event.event_id)
        raise EventSuppressed("staff became unavailable before reaching the assigned task")
    staff.zone_id = record["target_zone_id"]
    record["phase"] = "working"
    record["travel_due_minute"] = None
    record["arrival_minute"] = event.due_minute
    record["route_zone_ids"] = []
    record["route_edge_ids"] = []
    record["causation_event_ids"].append(event.event_id)


def complete_staff_operation(world: WorldState, event: ScheduledEvent) -> dict[str, Any]:
    from sheltersim.domain.population import admit_pending_arrivals
    from sheltersim.events.models import EventApplicationError

    state = world.model_state.get(STAFF_OPERATIONS_STATE_KEY)
    payload = event.payload
    staff_id, task_id = payload.get("staff_id"), payload.get("task_id")
    record = (
        state["staff"].get(staff_id)
        if isinstance(state, dict) and isinstance(staff_id, str)
        else None
    )
    staff = world.staff.get(staff_id) if isinstance(staff_id, str) else None
    task = world.active_tasks.get(task_id) if isinstance(task_id, str) else None
    if (
        record is None
        or record["phase"] != "working"
        or record["task_id"] != task_id
        or staff is None
        or task is None
        or staff.active_task_id != task_id
        or event.due_minute != record["due_minute"]
        or payload.get("task_kind") != record["task_kind"]
        or payload.get("target_zone_id") != record["target_zone_id"]
    ):
        raise EventApplicationError("staff operation completion does not match active work")
    if not staff_is_available(world, staff_id):
        interrupt_staff_operation(world, staff_id, event, current_event_id=event.event_id)
        raise EventSuppressed("staff became unavailable before completing the assigned task")
    kind, zone_id, target_id = record["task_kind"], record["target_zone_id"], record["target_id"]
    outcome = "completed"
    if kind == "cleaning":
        recovery = (
            state["config"]["effects_by_zone"]
            .get(zone_id, {})
            .get(
                "cleaning_sanitation_recovery",
                state["config"]["cleaning_sanitation_recovery"],
            )
        )
        state["zones"][zone_id]["sanitation"] = min(
            1.0, state["zones"][zone_id]["sanitation"] + recovery
        )
    elif kind == "maintenance" and target_id in world.facilities:
        facility = world.facilities[target_id]
        if facility.working_units >= facility.capacity or _maintenance_blocked(world, target_id):
            outcome = "blocked"
        else:
            facility.working_units += 1
            infrastructure = world.model_state.get("infrastructure_v1")
            if isinstance(infrastructure, dict):
                infrastructure["base_working_units"][target_id] = facility.working_units
    elif kind in {"information_outreach", "patrol"}:
        body = world.model_state.get("body_state_v1", {}).get("agents", {})
        effect_name = (
            "outreach_uncertainty_relief"
            if kind == "information_outreach"
            else "patrol_reassurance"
        )
        relief = (
            state["config"]["effects_by_zone"]
            .get(zone_id, {})
            .get(effect_name, state["config"][effect_name])
        )
        for agent in sorted(world.agents.values(), key=lambda item: item.id):
            if agent.zone_id != zone_id:
                continue
            record_body = body.get(agent.id)
            if record_body is not None:
                record_body["uncertainty"] = max(0.0, record_body["uncertainty"] - relief)
                record_body["reassurance"] = min(1.0, record_body["reassurance"] + relief * 0.5)
                record_body["positive_affect"] = min(
                    1.0, record_body["positive_affect"] + relief * 0.25
                )
            agent.trust = min(1.0, agent.trust + relief * 0.1)
            record["causation_event_ids"].append(event.event_id)
    elif kind == "intake":
        admit_pending_arrivals(world, world.minute_index)
    completed_operation = {
        "task_id": task_id,
        "staff_id": staff_id,
        "task_kind": kind,
        "target_id": target_id,
        "target_zone_id": zone_id,
        "reason": record["reason"],
        "status": outcome,
        "start_minute": record["started_minute"],
        "arrival_minute": record["arrival_minute"]
        if record["arrival_minute"] is not None
        else record["started_minute"],
        "complete_minute": event.due_minute,
        "travel_minutes": record["travel_minutes"],
        "causation_event_ids": sorted(set(record["causation_event_ids"] + [event.event_id])),
    }
    state["history"].append(completed_operation)
    if len(state["history"]) > MAX_STAFF_HISTORY:
        del state["history"][: len(state["history"]) - MAX_STAFF_HISTORY]
    del world.active_tasks[task_id]
    staff.work_queue.clear()
    staff.active_task_id = None
    staff.busy_until = 0
    state["staff"][staff_id] = _idle_record()
    return deepcopy(completed_operation)


def interrupt_staff_trips_on_closed_routes(
    world: WorldState, event: ScheduledEvent
) -> tuple[str, ...]:
    """Stop travel tasks invalidated by a newly applied closure for same-tick reassignment.

    The caller invokes this only after the typed geometry event was applied. Existing
    interruption logic records the incomplete work and moves staff to the nearest
    reached zone. The ordinary allocator then considers the work again using the
    refreshed, closure-aware graph.
    """
    from sheltersim.domain.movement import route_is_physically_clear

    state = world.model_state.get(STAFF_OPERATIONS_STATE_KEY)
    if not isinstance(state, dict):
        return ()
    interrupted: list[str] = []
    for staff_id, record in sorted(state["staff"].items()):
        if record.get("phase") != "travel":
            continue
        blocked = any(
            edge_id not in world.zone_edges
            or world.zone_edges[edge_id].closed
            or not route_is_physically_clear(world, edge_id, mobility="independent")
            for edge_id in record.get("route_edge_ids", ())
        )
        if blocked and interrupt_staff_operation(
            world, staff_id, event, current_event_id=event.event_id
        ):
            interrupted.append(staff_id)
    if interrupted and "movement_v1" in world.model_state:
        from sheltersim.domain.movement import staff_position_snapshots

        staff_position_snapshots(world, boundary_minute=event.due_minute)
    return tuple(interrupted)


def interrupt_staff_operation(
    world: WorldState,
    staff_id: str,
    event: ScheduledEvent,
    *,
    current_event_id: str | None = None,
) -> bool:
    """Cancel an autonomous task and its pending events at an unavailability boundary."""
    state = world.model_state.get(STAFF_OPERATIONS_STATE_KEY)
    staff = world.staff.get(staff_id)
    record = state["staff"].get(staff_id) if isinstance(state, dict) else None
    if staff is None or not isinstance(record, dict) or record.get("phase") == "idle":
        return False
    task_id = record["task_id"]
    if record["phase"] == "travel":
        elapsed = max(0, min(record["travel_minutes"], event.due_minute - record["started_minute"]))
        cumulative = 0
        nearest_zone = record["route_zone_ids"][0]
        for index, edge_id in enumerate(record["route_edge_ids"]):
            edge_minutes = world.zone_edges[edge_id].travel_minutes
            if edge_minutes and elapsed < cumulative + edge_minutes:
                progress = (elapsed - cumulative) / edge_minutes
                nearest_zone = (
                    record["route_zone_ids"][index]
                    if progress < 0.5
                    else record["route_zone_ids"][index + 1]
                )
                break
            cumulative += edge_minutes
            nearest_zone = record["route_zone_ids"][index + 1]
            if elapsed <= cumulative:
                break
        staff.zone_id = nearest_zone

    causation_ids = set(record["causation_event_ids"])
    causation_ids.add(event.event_id)
    for pending in world.event_queue.pending():
        if (
            pending.event_id == current_event_id
            or pending.event_type not in {"staff_operation_arrive", "staff_operation_complete"}
            or pending.payload.get("task_id") != task_id
        ):
            continue
        world.event_queue.remove(pending.event_id)
        causation_ids.add(pending.event_id)
        world.event_log.append(
            EventLogRecord(
                event=pending,
                realized_minute=event.due_minute,
                status=EventStatus.SUPPRESSED,
                detail="Staff operation was interrupted by staff unavailability",
            )
        )

    elapsed = max(0, min(record["travel_minutes"], event.due_minute - record["started_minute"]))
    arrival_minute = record["arrival_minute"]
    if (
        arrival_minute is None
        and record["travel_minutes"] > 0
        and elapsed >= record["travel_minutes"]
    ):
        arrival_minute = record["started_minute"] + record["travel_minutes"]
    state["history"].append(
        {
            "task_id": task_id,
            "staff_id": staff_id,
            "task_kind": record["task_kind"],
            "target_id": record["target_id"],
            "target_zone_id": record["target_zone_id"],
            "reason": record["reason"],
            "status": "interrupted",
            "start_minute": record["started_minute"],
            "arrival_minute": arrival_minute,
            "complete_minute": event.due_minute,
            "travel_minutes": record["travel_minutes"],
            "causation_event_ids": sorted(causation_ids),
        }
    )
    if len(state["history"]) > MAX_STAFF_HISTORY:
        del state["history"][: len(state["history"]) - MAX_STAFF_HISTORY]
    world.active_tasks.pop(task_id, None)
    staff.work_queue.clear()
    staff.active_task_id = None
    staff.busy_until = 0
    state["staff"][staff_id] = _idle_record()
    if "movement_v1" in world.model_state:
        from sheltersim.domain.movement import staff_position_snapshots

        staff_position_snapshots(world, boundary_minute=event.due_minute)
    return True


def _maintenance_blocked(world: WorldState, facility_id: str) -> bool:
    from sheltersim.domain.infrastructure import active_facility_outage
    from sheltersim.events.effects import has_active_facility_modifier

    return active_facility_outage(world, facility_id) or has_active_facility_modifier(
        world, facility_id
    )


def _idle_record() -> dict[str, Any]:
    return {
        "phase": "idle",
        "task_id": None,
        "task_kind": None,
        "target_id": None,
        "target_zone_id": None,
        "reason": None,
        "started_minute": None,
        "travel_due_minute": None,
        "arrival_minute": None,
        "due_minute": None,
        "route_zone_ids": [],
        "route_edge_ids": [],
        "travel_minutes": 0,
        "causation_event_ids": [],
    }
