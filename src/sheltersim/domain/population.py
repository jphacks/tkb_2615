"""Dynamic arrivals, capacity-aware admission, and departure history."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from sheltersim import native
from sheltersim.contracts.extensions import PersonTraits
from sheltersim.contracts.population import (
    DynamicArrivalPersonContext,
    PersonContext,
)
from sheltersim.contracts.presence import (
    PRESENCE_NAMESPACE,
    PresenceRecord,
    PresenceState,
    PresenceTransition,
)
from sheltersim.core.kernels import kernel_first
from sheltersim.core.state_mirror import register_rust_validator
from sheltersim.core.world import WorldState
from sheltersim.domain.agent_dynamics import (
    add_agent as add_agent_dynamics,
)
from sheltersim.domain.agent_dynamics import (
    remove_agent as remove_agent_dynamics,
)
from sheltersim.domain.body_state import add_body_agent, remove_body_agent
from sheltersim.domain.geometry_overlay import parsed_facility_geometry
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
from sheltersim.domain.models import (
    Activity,
    AgentProfile,
    AgentState,
    InitialNeedProfile,
    Mobility,
    NeedState,
)
from sheltersim.domain.movement import MOVEMENT_STATE_KEY, finish_movement
from sheltersim.domain.social_network import remove_agent as remove_social_relationships
from sheltersim.events.models import (
    EventApplicationError,
    EventKind,
    EventLogRecord,
    EventStatus,
    EventSuppressed,
    ScheduledEvent,
)

POPULATION_DYNAMICS_STATE_KEY = "population_dynamics_v1"
POPULATION_DYNAMICS_POLICY_VERSION = "capacity-aware-arrivals-departures-v1"
POPULATION_SOURCE_ID = "ASM-POPULATION-DYNAMICS-V1"
MAX_ARRIVALS_PER_EVENT = 1000
MAX_PENDING_ARRIVALS = 10_000


def create_population_dynamics_state(world: WorldState) -> dict[str, Any]:
    """Capture initial synthetic traits and prepare immutable population history."""
    traits: dict[str, dict[str, Any]] = {}
    raw = world.model_configs.get("population")
    if raw is not None:
        from sheltersim.contracts.extensions import PopulationSpec

        population = PopulationSpec.model_validate(raw)
        traits = {
            agent_id: item.model_dump(mode="json")
            for agent_id, item in sorted(population.people.items())
        }
    return {
        "schema_version": 1,
        "policy_version": POPULATION_DYNAMICS_POLICY_VERSION,
        "source_id": POPULATION_SOURCE_ID,
        "initial_agent_ids": sorted(world.agents),
        "profile_traits": traits,
        "profile_context": {},
        "pending_arrivals": [],
        "arrivals": [],
        "departures": [],
    }


# The state with no one pending: native/src/rules/tick_validation.rs.
register_rust_validator(
    POPULATION_DYNAMICS_STATE_KEY,
    native.rules("tick_validation").population_dynamics if native.available() else None,
)


@kernel_first("population", "validate_population_dynamics_state")
def validate_population_dynamics_state(world: WorldState) -> None:
    """Validate dynamic profile snapshots, admission order, and immutable history."""
    state = world.model_state.get(POPULATION_DYNAMICS_STATE_KEY)
    if state is None:
        return
    legacy_expected = {
        "schema_version",
        "policy_version",
        "source_id",
        "initial_agent_ids",
        "profile_traits",
        "pending_arrivals",
        "arrivals",
        "departures",
    }
    expected = legacy_expected | {"profile_context"}
    if (
        not isinstance(state, dict)
        or set(state) not in (legacy_expected, expected)
        or state.get("schema_version") != 1
        or state.get("policy_version") != POPULATION_DYNAMICS_POLICY_VERSION
        or state.get("source_id") != POPULATION_SOURCE_ID
        or not isinstance(state.get("initial_agent_ids"), list)
        or len(state["initial_agent_ids"]) != len(set(state["initial_agent_ids"]))
        or not isinstance(state.get("profile_traits"), dict)
        or not isinstance(state.get("pending_arrivals"), list)
        or not isinstance(state.get("arrivals"), list)
        or not isinstance(state.get("departures"), list)
        or len(state["pending_arrivals"]) > MAX_PENDING_ARRIVALS
    ):
        raise ValueError("population_dynamics_v1 has an invalid exact schema")
    if not set(world.agents) <= set(state["profile_traits"]) | set(state["initial_agent_ids"]):
        raise ValueError("population_dynamics_v1 is missing an active profile")
    profile_context = state.get("profile_context", {})
    historical_ids = set(state["initial_agent_ids"]) | set(state["profile_traits"])
    if not isinstance(profile_context, dict) or not set(profile_context) <= historical_ids:
        raise ValueError("population_dynamics_v1 has an invalid dynamic context index")
    for identity, raw_context in profile_context.items():
        context = PersonContext.model_validate(raw_context)
        related_ids = {
            *context.parent_ids,
            *context.caregiver_ids,
            *context.care_for_ids,
            *context.partner_ids,
            *context.relative_ids,
        }
        if identity in related_ids or not related_ids <= historical_ids:
            raise ValueError("dynamic household context references an unknown person")
        traits = state["profile_traits"].get(identity, {})
        if (
            POPULATION_SOURCE_ID not in context.source_ids
            or context.household_id is None
            or context.household_id != traits.get("household_id")
            or any(
                state["profile_traits"].get(related_id, {}).get("household_id")
                != context.household_id
                for related_id in related_ids
            )
        ):
            raise ValueError(
                "dynamic household context disagrees with generated profile provenance"
            )
    seen_pending: set[str] = set()
    pending_context_fields = {"member_index", "care_support_needs", "context_source_id"}
    approach_fields = {"approach_route_id", "approach_started_minute", "approach_due_minute"}
    pending_base_fields = {"profile", "traits", "zone_id", "created_minute", "event_id"}
    valid_pending_fields = {
        frozenset(pending_base_fields | context | approach | admission)
        for context in (set(), pending_context_fields)
        for approach in (
            set(),
            approach_fields,
            approach_fields | {"approach_completion_event_id"},
            approach_fields | {"approach_completed_minute"},
        )
        for admission in (set(), {"admission_zone_ids"})
    }
    for pending in state["pending_arrivals"]:
        if (
            not isinstance(pending, dict)
            or frozenset(pending) not in valid_pending_fields
            or pending["zone_id"] not in world.zones
            or pending["profile"].get("id") in seen_pending
            or pending["profile"].get("id") in world.agents
            or (
                "admission_zone_ids" in pending
                and (
                    not isinstance(pending["admission_zone_ids"], list)
                    or any(
                        not isinstance(zone_id, str) for zone_id in pending["admission_zone_ids"]
                    )
                    or len(pending["admission_zone_ids"]) != len(set(pending["admission_zone_ids"]))
                    or not set(pending["admission_zone_ids"]) <= set(world.zones)
                )
            )
        ):
            raise ValueError("population_dynamics_v1 pending arrival is invalid")
        if pending_context_fields <= set(pending) and (
            type(pending["member_index"]) is not int
            or not 0 <= pending["member_index"] < MAX_ARRIVALS_PER_EVENT
            or not isinstance(pending["care_support_needs"], list)
            or any(not isinstance(item, str) for item in pending["care_support_needs"])
            or len(pending["care_support_needs"]) != len(set(pending["care_support_needs"]))
            or not isinstance(pending["context_source_id"], str)
        ):
            raise ValueError("population_dynamics_v1 pending context is invalid")
        if (
            pending_context_fields <= set(pending)
            and list(
                profile_context.get(pending["profile"]["id"], {}).get("care_support_needs", ())
            )
            != pending["care_support_needs"]
        ):
            raise ValueError("pending care context disagrees with its persisted profile context")
        if approach_fields <= set(pending):
            if (
                pending["approach_route_id"]
                not in {
                    route.route_id
                    for route in parsed_facility_geometry(
                        world.model_configs.get("facility_geometry", {})
                    ).arrival_approach_routes
                }
                or type(pending["approach_started_minute"]) is not int
                or type(pending["approach_due_minute"]) is not int
                or pending["approach_started_minute"] != pending["created_minute"]
                or pending["approach_due_minute"] <= pending["approach_started_minute"]
            ):
                raise ValueError("pending arrival approach route/timing is invalid")
            route = next(
                item
                for item in parsed_facility_geometry(
                    world.model_configs.get("facility_geometry", {})
                ).arrival_approach_routes
                if item.route_id == pending["approach_route_id"]
            )
            if (
                route.destination_zone_id != pending["zone_id"]
                or pending["approach_due_minute"]
                != pending["approach_started_minute"] + route.travel_minutes
            ):
                raise ValueError("pending arrival approach disagrees with its route contract")
            completed = pending.get("approach_completed_minute")
            if completed is not None and (
                type(completed) is not int
                or completed < pending["approach_due_minute"]
                or completed > world.minute_index
            ):
                raise ValueError("pending arrival approach completion minute is invalid")
            completion_event_id = pending.get("approach_completion_event_id")
            if completed is None and pending["approach_due_minute"] < world.duration_minutes:
                matching_events = [
                    event
                    for event in world.event_queue.pending()
                    if event.event_id == completion_event_id
                    and event.event_type == "population_approach_complete"
                ]
                if completion_event_id is None or len(matching_events) != 1:
                    raise ValueError("pending arrival approach completion event is missing")
                completion_event = matching_events[0]
                if (
                    completion_event.due_minute != pending["approach_due_minute"]
                    or completion_event.payload.get("agent_id") != pending["profile"]["id"]
                    or completion_event.payload.get("route_id") != pending["approach_route_id"]
                ):
                    raise ValueError("pending arrival approach completion event disagrees")
            elif completed is None and completion_event_id is not None:
                raise ValueError("out-of-horizon arrival approach has a completion event")
            elif completed is not None and completion_event_id is not None:
                raise ValueError("completed arrival approach retains a pending event")
            presence = (
                world.model_state.get(PRESENCE_NAMESPACE, {})
                .get("people", {})
                .get(pending["profile"]["id"], {})
            )
            expected_presence_status = "approaching" if completed is None else "awaiting_admission"
            if presence and presence.get("status") != expected_presence_status:
                raise ValueError("arrival approach disagrees with population presence")
        elif "approach_completed_minute" in pending:
            raise ValueError("pending arrival approach completion has no route")
        seen_pending.add(pending["profile"]["id"])
        PersonTraits.model_validate(pending["traits"])


@kernel_first("population", "queue_arrivals")
def queue_arrivals(
    world: WorldState,
    *,
    event: ScheduledEvent,
    zone_id: str,
    count: int,
    household_size: int = 1,
    mobility: str = "independent",
    language: str = "ja",
    admission_zone_ids: tuple[str, ...] = (),
    approach_route_id: str | None = None,
    person_context_overrides: tuple[dict[str, Any], ...] = (),
) -> int:
    """Create deterministic synthetic profiles, admitting only within zone capacity."""
    if zone_id not in world.zones:
        raise EventApplicationError("population arrival references an unknown zone")
    if (
        not isinstance(admission_zone_ids, (list, tuple))
        or any(not isinstance(item, str) for item in admission_zone_ids)
        or len(admission_zone_ids) != len(set(admission_zone_ids))
        or any(item not in world.zones for item in admission_zone_ids)
        or zone_id in admission_zone_ids
    ):
        raise EventApplicationError("population arrival alternate admission zones are invalid")
    approach_route = None
    if approach_route_id is not None:
        if admission_zone_ids:
            raise EventApplicationError(
                "one arrival approach route cannot combine with alternate admission zones"
            )
        try:
            geometry = parsed_facility_geometry(world.model_configs.get("facility_geometry", {}))
            approach_route = next(
                item
                for item in geometry.arrival_approach_routes
                if item.route_id == approach_route_id
            )
        except (StopIteration, TypeError, ValueError) as exc:
            raise EventApplicationError("population arrival approach route is invalid") from exc
        if approach_route.destination_zone_id != zone_id:
            raise EventApplicationError(
                "population arrival approach route targets a different zone"
            )
    overlay_config = world.model_configs.get("geometry_overlay", {})
    if admission_zone_ids and (
        not isinstance(overlay_config, dict) or overlay_config.get("schema_version") not in {3, 4}
    ):
        raise EventApplicationError(
            "alternate room admission requires geometry_overlay schema_version=3 or 4"
        )
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or not 1 <= count <= MAX_ARRIVALS_PER_EVENT
    ):
        raise EventApplicationError("population arrival count is outside its supported range")
    if (
        not isinstance(household_size, int)
        or isinstance(household_size, bool)
        or not 1 <= household_size <= 12
    ):
        raise EventApplicationError(
            "population arrival household_size is outside its supported range"
        )
    try:
        mobility_value = Mobility(mobility)
    except ValueError as exc:
        raise EventApplicationError("population arrival mobility is invalid") from exc
    try:
        PersonTraits(age_years=30, household_role="adult", languages=(language,))
    except ValueError as exc:
        raise EventApplicationError("population arrival language is invalid") from exc
    try:
        context_overrides = tuple(
            DynamicArrivalPersonContext.model_validate(item) for item in person_context_overrides
        )
    except (TypeError, ValueError) as exc:
        raise EventApplicationError("population arrival person context is invalid") from exc
    if len({item.member_index for item in context_overrides}) != len(context_overrides) or any(
        item.member_index >= count for item in context_overrides
    ):
        raise EventApplicationError("population arrival person context index is invalid")
    context_by_index = {item.member_index: item for item in context_overrides}
    state = _state(world)
    if len(state["pending_arrivals"]) + count > MAX_PENDING_ARRIVALS:
        raise EventApplicationError("population arrival exceeds the pending capacity bound")
    if len(world.agents) + len(state["pending_arrivals"]) + count > 10_000:
        raise EventApplicationError("population arrival exceeds the 10000-resident runtime bound")
    known_ids = set(state["initial_agent_ids"]) | set(state["profile_traits"])
    if len(known_ids) + count > 10_000:
        raise EventApplicationError("population arrival exceeds the 10000-profile history bound")
    reserved_ids = (
        set(world.agents)
        | {item["profile"]["id"] for item in state["pending_arrivals"]}
        | set(state["profile_traits"])
    )
    prepared: list[dict[str, Any]] = []
    event_digest = hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:20]
    for index in range(count):
        digest = hashlib.sha256(
            f"{world.seed}\0{event.event_id}\0{index}".encode("utf-8")
        ).hexdigest()
        agent_id = f"arrival-{digest[:28]}"
        if agent_id in reserved_ids:
            raise EventApplicationError("population arrival generated a duplicate stable ID")
        group_index = index // household_size
        group_id = f"household-{event_digest}-{group_index:03d}"
        age_years = 5 + int(digest[0:2], 16) % 79
        role = (
            "child"
            if age_years < 18
            else "dependent"
            if age_years >= 75
            else "alone"
            if household_size == 1
            else "adult"
        )
        traits = PersonTraits(
            age_years=age_years,
            household_id=group_id,
            household_role=role,
            languages=(language,),
        ).model_dump(mode="json")
        need_values = {
            "toilet_urgency": int(digest[2:4], 16) / 255.0 * 0.35,
            "hunger": int(digest[4:6], 16) / 255.0 * 0.35,
            "fatigue": int(digest[6:8], 16) / 255.0 * 0.35,
            "hydration_need": (
                int(digest[8:10], 16) / 255.0 * 0.35 if world.schema_version == 6 else 0.0
            ),
        }
        initial_needs = InitialNeedProfile(**need_values)
        profile = AgentProfile(
            id=agent_id,
            group_id=group_id,
            initial_zone_id=zone_id,
            mobility=mobility_value,
            support_need=int(digest[10:12], 16) / 255.0 * 0.5,
            stress_sensitivity=int(digest[12:14], 16) / 255.0,
            rng_key=f"arrival:{digest[:24]}",
            initial_needs=initial_needs,
            provenance={
                "source_type": "assumption",
                "source_id": POPULATION_SOURCE_ID,
                "event_id": event.event_id,
                "seed": world.seed,
            },
        )
        prepared.append(
            {
                "profile": {
                    "id": profile.id,
                    "group_id": profile.group_id,
                    "initial_zone_id": profile.initial_zone_id,
                    "mobility": profile.mobility.value,
                    "support_need": profile.support_need,
                    "stress_sensitivity": profile.stress_sensitivity,
                    "rng_key": profile.rng_key,
                    "initial_needs": {
                        name: getattr(initial_needs, name)
                        for name in (
                            ("toilet_urgency", "hunger", "fatigue", "hydration_need")
                            if world.schema_version == 6
                            else ("toilet_urgency", "hunger", "fatigue")
                        )
                    },
                    "provenance": profile.provenance,
                },
                "traits": traits,
                "member_index": index,
                "care_support_needs": list(
                    context_by_index[index].care_support_needs if index in context_by_index else ()
                ),
                "context_source_id": (
                    event.source_id if isinstance(event.source_id, str) else POPULATION_SOURCE_ID
                ),
                "zone_id": zone_id,
                "admission_zone_ids": list(admission_zone_ids),
                "created_minute": event.due_minute,
                "event_id": event.event_id,
                **(
                    {
                        "approach_route_id": approach_route.route_id,
                        "approach_started_minute": event.due_minute,
                        "approach_due_minute": (event.due_minute + approach_route.travel_minutes),
                    }
                    if approach_route is not None
                    else {}
                ),
            }
        )
        reserved_ids.add(agent_id)
    state.setdefault("profile_context", {}).update(_generated_arrival_context(prepared))
    admitted = 0
    for item in prepared:
        state["profile_traits"][item["profile"]["id"]] = item["traits"]
        if approach_route is not None:
            _schedule_arrival_approach_completion(world, item, event, approach_route)
            state["pending_arrivals"].append(item)
            _record_waiting_presence(world, item)
            continue
        destination = _choose_admission_target(world, item)
        if destination is not None:
            destination_zone_id, room_id = destination
            _admit_profile(
                world,
                item,
                event.due_minute,
                admitted_zone_id=destination_zone_id,
                room_id=room_id,
            )
            admitted += 1
        else:
            state["pending_arrivals"].append(item)
            _record_waiting_presence(world, item)
    state["arrivals"].append(
        {
            "event_id": event.event_id,
            "zone_id": zone_id,
            "requested_count": count,
            "admitted_count": admitted,
            "waiting_count": count - admitted,
            "minute": event.due_minute,
        }
    )
    world.metric_state["population_arrival_count"] = (
        world.metric_state.get("population_arrival_count", 0.0) + admitted
    )
    world.metric_state["population_waiting_count"] = float(len(state["pending_arrivals"]))
    return admitted


def _schedule_arrival_approach_completion(
    world: WorldState,
    item: dict[str, Any],
    arrival_event: ScheduledEvent,
    route: Any,
) -> None:
    due_minute = item["approach_due_minute"]
    if due_minute >= world.duration_minutes:
        return
    program_id = arrival_event.payload.get("program_id")
    effect_id = arrival_event.payload.get("effect_id")
    effect_state = world.model_state.get("effect_program_v1", {})
    program = effect_state.get("programs", {}).get(program_id)
    if (
        not isinstance(program, dict)
        or effect_id not in program.get("effect_ids", [])
        or program.get("operators", {}).get(effect_id) != "add_evacuees"
    ):
        raise EventApplicationError("arrival approach is not part of an approved EffectProgram")
    sequence = world.allocate_event_sequence()
    completion_event_id = f"arrival-approach-complete-{sequence}"
    item["approach_completion_event_id"] = completion_event_id
    marker = arrival_event.to_dict()["provenance"].get("effect_program_v1")
    if not isinstance(marker, Mapping):
        raise EventApplicationError("arrival event lacks its approved EffectProgram provenance")
    derived_marker = {**dict(marker), "phase": "derived"}
    completion = ScheduledEvent(
        event_id=completion_event_id,
        kind=EventKind.COMPLETION,
        due_minute=due_minute,
        priority=0,
        sequence=sequence,
        event_type="population_approach_complete",
        payload={
            "program_id": program_id,
            "effect_id": effect_id,
            "agent_id": item["profile"]["id"],
            "arrival_event_id": arrival_event.event_id,
            "route_id": route.route_id,
            "zone_id": item["zone_id"],
        },
        source_id=route.source_ids[0],
        causation_id=arrival_event.event_id,
        provenance={
            "effect_program_v1": derived_marker,
            "arrival_approach": {
                "route_id": route.route_id,
                "source_ids": list(route.source_ids),
                "walking_speed_m_per_min": route.walking_speed_m_per_min,
                "distance_m": route.distance_m,
            },
        },
    )
    world.schedule_event(completion)


@kernel_first("population", "apply_arrival_approach_completion")
def apply_arrival_approach_completion(world: WorldState, event: ScheduledEvent) -> None:
    """Commit the APPLIED arrival at the entrance and retain its event-linked history."""
    expected = {"program_id", "effect_id", "agent_id", "arrival_event_id", "route_id", "zone_id"}
    if event.event_type != "population_approach_complete" or set(event.payload) != expected:
        raise EventApplicationError("arrival approach completion payload fields do not match")
    program_state = world.model_state.get("effect_program_v1", {})
    program = program_state.get("programs", {}).get(event.payload["program_id"])
    if (
        not isinstance(program, dict)
        or event.payload["effect_id"] not in program.get("effect_ids", [])
        or program.get("operators", {}).get(event.payload["effect_id"]) != "add_evacuees"
        or event.causation_id != event.payload["arrival_event_id"]
    ):
        raise EventApplicationError("arrival approach completion is outside its approved program")
    state = world.model_state.get(POPULATION_DYNAMICS_STATE_KEY)
    pending = (
        next(
            (
                item
                for item in state.get("pending_arrivals", [])
                if item["profile"]["id"] == event.payload["agent_id"]
            ),
            None,
        )
        if isinstance(state, dict)
        else None
    )
    if (
        pending is None
        or pending.get("approach_completion_event_id") != event.event_id
        or pending.get("approach_route_id") != event.payload["route_id"]
        or pending.get("event_id") != event.payload["arrival_event_id"]
        or pending.get("zone_id") != event.payload["zone_id"]
        or pending.get("approach_due_minute") != event.due_minute
        or pending.get("approach_completed_minute") is not None
    ):
        raise EventApplicationError("arrival approach completion does not match pending arrival")
    route = next(
        (
            item
            for item in parsed_facility_geometry(
                world.model_configs.get("facility_geometry", {})
            ).arrival_approach_routes
            if item.route_id == event.payload["route_id"]
        ),
        None,
    )
    if (
        route is None
        or route.destination_zone_id != event.payload["zone_id"]
        or event.source_id not in route.source_ids
    ):
        raise EventApplicationError("arrival approach route provenance does not match")
    pending.pop("approach_completion_event_id")
    pending["approach_completed_minute"] = event.due_minute
    _record_approach_completed(world, pending, event)


def _generated_arrival_context(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Link only age-role-compatible members of an explicitly grouped synthetic household."""
    households: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        traits = item["traits"]
        households.setdefault(traits["household_id"], []).append(item)
    contexts: dict[str, dict[str, Any]] = {}
    for household_id, members in sorted(households.items()):
        members.sort(key=lambda item: item["profile"]["id"])
        adults = [
            item["profile"]["id"] for item in members if item["traits"]["household_role"] == "adult"
        ]
        caregiver_id = adults[0] if adults else None
        dependents = [
            item for item in members if item["traits"]["household_role"] in {"child", "dependent"}
        ]
        care_for_ids = (
            tuple(sorted(item["profile"]["id"] for item in dependents))
            if caregiver_id is not None
            else ()
        )
        for item in members:
            identity = item["profile"]["id"]
            role = item["traits"]["household_role"]
            caregiver_ids = (
                (caregiver_id,)
                if caregiver_id is not None and role in {"child", "dependent"}
                else ()
            )
            requires_review = role in {"child", "dependent"} or bool(care_for_ids)
            reasons = (
                ("synthetic_arrival_care_relationship_requires_review",) if requires_review else ()
            )
            roles = (
                ("caregiver",)
                if role == "adult" and identity == caregiver_id and care_for_ids
                else ("child",)
                if role == "child"
                else ("dependent",)
                if role == "dependent"
                else ("alone",)
                if role == "alone"
                else ()
            )
            context = PersonContext(
                household_id=household_id,
                caregiver_ids=caregiver_ids,
                care_for_ids=care_for_ids if identity == caregiver_id else (),
                requires_review=requires_review,
                review_reasons=reasons,
                roles=roles,
                guardian_status=("accompanied" if caregiver_ids else "unaccompanied")
                if role == "child"
                else None,
                care_support_needs=tuple(item["care_support_needs"]),
                source_ids=tuple(
                    sorted(
                        {
                            POPULATION_SOURCE_ID,
                            item["context_source_id"],
                        }
                    )
                ),
            )
            contexts[identity] = context.model_dump(
                mode="json", exclude_defaults=True, exclude_none=True
            )
    return contexts


@kernel_first("population", "admit_pending_arrivals")
def admit_pending_arrivals(world: WorldState, minute: int) -> tuple[str, ...]:
    """Admit waiting synthetic residents in event/ID order when space opens."""
    state = world.model_state.get(POPULATION_DYNAMICS_STATE_KEY)
    if state is None or not state["pending_arrivals"]:
        return ()
    admitted: list[str] = []
    remaining = []
    for item in sorted(
        state["pending_arrivals"],
        key=lambda value: (value["created_minute"], value["event_id"], value["profile"]["id"]),
    ):
        if "approach_route_id" in item:
            if item.get("approach_completed_minute") is None:
                remaining.append(item)
                continue
        destination = _choose_admission_target(world, item)
        if destination is not None:
            destination_zone_id, room_id = destination
            _admit_profile(
                world,
                item,
                minute,
                admitted_zone_id=destination_zone_id,
                room_id=room_id,
            )
            admitted.append(item["profile"]["id"])
            item["admitted_minute"] = minute
        else:
            remaining.append(item)
    pending = state["pending_arrivals"]
    # Everyone stays where they were on most ticks, and a new list is converted again.
    if len(remaining) != len(pending) or any(a is not b for a, b in zip(remaining, pending)):
        state["pending_arrivals"] = remaining
    if admitted:
        world.metric_state["population_arrival_count"] = world.metric_state.get(
            "population_arrival_count", 0.0
        ) + len(admitted)
    world.metric_state["population_waiting_count"] = float(len(remaining))
    for record in state["arrivals"]:
        # This list contains individual admission receipts and event summaries.
        if "requested_count" not in record:
            continue
        waiting_ids = {
            item["profile"]["id"] for item in remaining if item["event_id"] == record["event_id"]
        }
        record["waiting_count"] = len(waiting_ids)
        record["admitted_count"] = record["requested_count"] - len(waiting_ids)
    return tuple(admitted)


@kernel_first("population", "queue_departures")
def queue_departures(
    world: WorldState,
    *,
    event: ScheduledEvent,
    zone_id: str,
    count: int,
    include_initial: bool = False,
) -> int:
    """Remove a deterministic local set of residents, preserving stable departure records."""
    if zone_id not in world.zones:
        raise EventApplicationError("population departure references an unknown zone")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or not 1 <= count <= MAX_ARRIVALS_PER_EVENT
    ):
        raise EventApplicationError("population departure count is outside its supported range")
    if not isinstance(include_initial, bool):
        raise EventApplicationError("population departure include_initial must be boolean")
    state = _state(world)
    initial_ids = set(state["initial_agent_ids"])
    candidates = [
        agent_id
        for agent_id, agent in world.agents.items()
        if (include_initial or agent_id not in initial_ids) and agent.zone_id == zone_id
    ]
    candidates.sort(
        key=lambda agent_id: (
            _arrival_minute(state, agent_id) if agent_id not in initial_ids else -1,
            agent_id,
        )
    )
    if len(candidates) < count:
        raise EventApplicationError(
            f"only {len(candidates)} admitted dynamic residents are available to leave"
        )
    selected = candidates[:count]
    busy = [
        agent_id
        for agent_id in selected
        if world.agents[agent_id].activity
        not in (Activity.IDLE, Activity.RESTING, Activity.SLEEPING)
        or world.agents[agent_id].support_request_id is not None
    ]
    if busy:
        schedule_population_retry(world, event)
        raise EventSuppressed("departure waits for the selected residents' active tasks")
    for agent_id in selected:
        _depart_agent(world, agent_id, event, state)
    world.metric_state["population_departure_count"] = world.metric_state.get(
        "population_departure_count", 0.0
    ) + len(selected)
    world.metric_state["population_waiting_count"] = float(len(state["pending_arrivals"]))
    admit_pending_arrivals(world, world.minute_index)
    return len(selected)


def _depart_agent(
    world: WorldState, agent_id: str, event: ScheduledEvent, state: dict[str, Any]
) -> None:
    agent = world.agents[agent_id]
    from sheltersim.domain.geometry_overlay import release_sleep_bed

    release_sleep_bed(world, agent_id)
    from sheltersim.domain.geometry_overlay import release_temporary_room_admission

    release_temporary_room_admission(world, agent_id)
    state["departures"].append(
        {
            "event_id": event.event_id,
            "agent_id": agent_id,
            "zone_id": agent.zone_id,
            "minute": event.due_minute,
            "profile": _profile_record(agent.profile),
            "traits": state["profile_traits"].get(agent_id),
            "needs": {
                "toilet_urgency": agent.needs.toilet_urgency,
                "hunger": agent.needs.hunger,
                "fatigue": agent.needs.fatigue,
                "hydration_need": agent.needs.hydration_need,
            },
            "relationships": remove_social_relationships(world, agent_id),
        }
    )
    withdrawn_message_ids = _remove_information_state(world, agent_id)
    _cancel_agent_events(world, agent_id, event, withdrawn_message_ids)
    resource_service = world.model_state.get("resource_service_v1")
    if isinstance(resource_service, dict):
        resource_service["request_history"] = [
            item for item in resource_service["request_history"] if item["agent_id"] != agent_id
        ]
        resource_service["queue_stall_history"] = [
            item for item in resource_service["queue_stall_history"] if item["agent_id"] != agent_id
        ]
        resource_service["stall_state"] = {
            facility_id: item
            for facility_id, item in resource_service["stall_state"].items()
            if item["agent_id"] != agent_id
        }
    social_runtime = world.model_state.get("social_propagation_v1")
    if isinstance(social_runtime, dict):
        contacts = social_runtime.get("contacts", {})
        if isinstance(contacts, dict):
            social_runtime["contacts"] = {
                key: value for key, value in contacts.items() if agent_id not in json.loads(key)
            }
        cooldowns = social_runtime.get("agent_cooldown_until", {})
        if isinstance(cooldowns, dict):
            cooldowns.pop(agent_id, None)
    world.support_requests = {
        key: request
        for key, request in world.support_requests.items()
        if request.agent_id != agent_id
    }
    remove_agent_dynamics(world, agent_id)
    remove_body_agent(world, agent_id)
    if MOVEMENT_STATE_KEY in world.model_state:
        world.model_state[MOVEMENT_STATE_KEY]["journeys"].pop(agent_id, None)
        world.model_state[MOVEMENT_STATE_KEY]["positions"].pop(agent_id, None)
    world.agents.pop(agent_id)
    _record_presence_transition(world, agent_id, event.event_id, event.due_minute, "departed", None)


def _admit_profile(
    world: WorldState,
    item: dict[str, Any],
    minute: int,
    *,
    admitted_zone_id: str | None = None,
    room_id: str | None = None,
) -> None:
    raw = item["profile"]
    initial = dict(raw["initial_needs"])
    if world.schema_version == 5:
        initial["hydration_need"] = 0.0
    profile = AgentProfile(
        id=raw["id"],
        group_id=raw["group_id"],
        initial_zone_id=raw["initial_zone_id"],
        mobility=Mobility(raw["mobility"]),
        support_need=raw["support_need"],
        stress_sensitivity=raw["stress_sensitivity"],
        rng_key=raw["rng_key"],
        initial_needs=InitialNeedProfile(**initial),
        provenance=dict(raw["provenance"]),
    )
    agent = AgentState(
        profile=profile,
        zone_id=admitted_zone_id or item["zone_id"],
        needs=NeedState(**initial),
        next_decision_at=minute,
    )
    world.agents[agent.id] = agent
    _add_information_state(world, agent.id)
    add_agent_dynamics(world, agent.id)
    add_body_agent(world, agent.id)
    state = world.model_state[POPULATION_DYNAMICS_STATE_KEY]
    state["arrivals"].append(
        {
            "event_id": item["event_id"],
            "agent_id": agent.id,
            "zone_id": agent.zone_id,
            "preferred_zone_id": item["zone_id"],
            "room_id": room_id,
            "created_minute": item["created_minute"],
            "admitted_minute": minute,
            **(
                {
                    "approach_route_id": item["approach_route_id"],
                    "approach_started_minute": item["approach_started_minute"],
                    "approach_completed_minute": item.get(
                        "approach_completed_minute", item["approach_due_minute"]
                    ),
                }
                if "approach_route_id" in item
                else {}
            ),
        }
    )
    if MOVEMENT_STATE_KEY in world.model_state:
        world.model_state[MOVEMENT_STATE_KEY]["positions"][agent.id] = {
            "zone_id": agent.zone_id,
            "point": None,
            "edge_id": None,
            "progress": 0.0,
            "position_kind": "unavailable",
        }
        finish_movement(world, agent.id)
        if room_id is not None:
            from sheltersim.domain.geometry_overlay import admit_to_temporary_room

            point = admit_to_temporary_room(
                world,
                agent_id=agent.id,
                room_id=room_id,
                minute=minute,
                event_id=item["event_id"],
            )
            world.model_state[MOVEMENT_STATE_KEY]["positions"][agent.id].update(
                {
                    "zone_id": agent.zone_id,
                    "point": point.model_dump(mode="json"),
                    "edge_id": None,
                    "progress": 1.0,
                    "position_kind": "physical",
                }
            )
    elif room_id is not None:
        from sheltersim.domain.geometry_overlay import admit_to_temporary_room

        admit_to_temporary_room(
            world,
            agent_id=agent.id,
            room_id=room_id,
            minute=minute,
            event_id=item["event_id"],
        )
    _record_presence_transition(
        world,
        agent.id,
        item["event_id"],
        minute,
        (
            "admitted"
            if world.model_state.get(PRESENCE_NAMESPACE, {})
            .get("people", {})
            .get(agent.id, {})
            .get("status")
            == "awaiting_admission"
            else "arrived"
        ),
        agent.zone_id,
        household_id=(item.get("traits") or {}).get("household_id"),
        source_id=raw.get("provenance", {}).get("source_id", POPULATION_SOURCE_ID),
    )


def _record_waiting_presence(world: WorldState, item: dict[str, Any]) -> None:
    """Retain the stable identity and target zone before a capacity-limited admission."""
    raw = world.model_state.get(PRESENCE_NAMESPACE)
    if not isinstance(raw, dict):
        return
    PresenceState.model_validate(raw)
    profile = item["profile"]
    agent_id = profile["id"]
    if agent_id in raw["people"]:
        raise EventApplicationError("waiting arrival already has a presence record")
    household_id = (item.get("traits") or {}).get("household_id", profile.get("group_id"))
    route_sources: tuple[str, ...] = ()
    if "approach_route_id" in item:
        geometry = parsed_facility_geometry(world.model_configs["facility_geometry"])
        route = next(
            route
            for route in geometry.arrival_approach_routes
            if route.route_id == item["approach_route_id"]
        )
        route_sources = route.source_ids
    raw["people"][agent_id] = PresenceRecord(
        agent_id=agent_id,
        household_id=household_id,
        status=("approaching" if "approach_route_id" in item else "awaiting_admission"),
        since_minute=item["created_minute"],
        transition_zone_id=item["zone_id"],
        source_event_ids=(item["event_id"],),
    ).model_dump(mode="json")
    transition = PresenceTransition(
        transition_id="presence-"
        + hashlib.sha256(
            (
                f"{item['event_id']}\0{agent_id}\0"
                f"{('approaching' if 'approach_route_id' in item else 'waiting')}"
            ).encode("utf-8")
        ).hexdigest()[:24],
        event_id=item["event_id"],
        agent_id=agent_id,
        minute=item["created_minute"],
        action=("approaching" if "approach_route_id" in item else "waiting"),
        to_zone_id=item["zone_id"],
        to_household_id=household_id,
        source_ids=tuple(sorted({POPULATION_SOURCE_ID, *route_sources})),
    )
    _append_presence_history(raw, transition)


def _record_approach_completed(
    world: WorldState, item: dict[str, Any], event: ScheduledEvent
) -> None:
    """Move an inbound person from outdoor travel to the admission queue."""
    raw = world.model_state.get(PRESENCE_NAMESPACE)
    if not isinstance(raw, dict):
        return
    profile = item["profile"]
    agent_id = profile["id"]
    previous = raw.get("people", {}).get(agent_id)
    if not isinstance(previous, dict) or previous.get("status") != "approaching":
        raise EventApplicationError("arrival approach has no matching presence state")
    record = PresenceRecord.model_validate(previous).model_copy(
        update={"status": "awaiting_admission", "since_minute": event.due_minute}
    )
    raw["people"][agent_id] = record.model_dump(mode="json")
    transition = PresenceTransition(
        transition_id="presence-"
        + hashlib.sha256(
            f"{event.event_id}\0{agent_id}\0approach-completed".encode("utf-8")
        ).hexdigest()[:24],
        event_id=event.event_id,
        agent_id=agent_id,
        minute=event.due_minute,
        action="approach_completed",
        from_zone_id=None,
        to_zone_id=item["zone_id"],
        causation_id=event.causation_id,
        to_household_id=record.household_id,
        source_ids=tuple(
            sorted(
                {
                    POPULATION_SOURCE_ID,
                    *next(
                        route.source_ids
                        for route in parsed_facility_geometry(
                            world.model_configs["facility_geometry"]
                        ).arrival_approach_routes
                        if route.route_id == item["approach_route_id"]
                    ),
                }
            )
        ),
    )
    _append_presence_history(raw, transition)


def _record_presence_transition(
    world: WorldState,
    agent_id: str,
    event_id: str,
    minute: int,
    action: str,
    zone_id: str | None,
    *,
    household_id: str | None = None,
    source_id: str = POPULATION_SOURCE_ID,
) -> None:
    """Keep the shared presence ledger aligned with dynamic admission/departure."""
    raw = world.model_state.get(PRESENCE_NAMESPACE)
    if not isinstance(raw, dict):
        return
    PresenceState.model_validate(raw)
    previous_raw = raw["people"].get(agent_id)
    previous = PresenceRecord.model_validate(previous_raw) if previous_raw is not None else None
    agent = world.agents.get(agent_id)
    household = household_id
    if household is None and previous is not None:
        household = previous.household_id
    if household is None and agent is not None:
        household = agent.profile.group_id
    status = "admitted" if agent is not None else "departed"
    zone = zone_id or (
        agent.zone_id if agent is not None else previous.transition_zone_id if previous else None
    )
    record = PresenceRecord(
        agent_id=agent_id,
        household_id=household,
        status=status,
        since_minute=minute,
        transition_zone_id=zone,
        source_event_ids=tuple(
            sorted(set((previous.source_event_ids if previous else ()) + (event_id,)))
        ),
    )
    raw["people"][agent_id] = record.model_dump(mode="json")
    transition = PresenceTransition(
        transition_id="presence-"
        + hashlib.sha256(f"{event_id}\\0{agent_id}\\0{action}".encode("utf-8")).hexdigest()[:24],
        event_id=event_id,
        agent_id=agent_id,
        minute=minute,
        action=action,
        from_zone_id=previous.transition_zone_id if previous else None,
        to_zone_id=zone,
        from_household_id=previous.household_id if previous else None,
        to_household_id=household,
        source_ids=(source_id,),
    )
    _append_presence_history(raw, transition)


def _append_presence_history(raw: dict[str, Any], transition: PresenceTransition) -> None:
    # In place: a new list would be converted again, record by record, at every transition.
    raw["history"].append(transition.model_dump(mode="json"))
    del raw["history"][:-20_000]


def _add_information_state(world: WorldState, agent_id: str) -> None:
    state = world.model_state.get(INFORMATION_SPREAD_STATE_KEY)
    if state is not None:
        state["beliefs"][agent_id] = {}
        state["query_pressure"][agent_id] = 0.0


def _remove_information_state(world: WorldState, agent_id: str) -> set[str]:
    state = world.model_state.get(INFORMATION_SPREAD_STATE_KEY)
    if state is None:
        return set()
    archived_beliefs = state["beliefs"].pop(agent_id, {})
    del archived_beliefs
    state["query_pressure"].pop(agent_id, None)
    pair_keys = []
    for key in state["contacts"]:
        pair = json.loads(key)
        if agent_id in pair:
            pair_keys.append(key)
    for key in pair_keys:
        state["contacts"].pop(key, None)
    withdrawn = {
        message_id
        for message_id, message in state["messages"].items()
        if message.get("origin_agent_id") == agent_id
    }
    for message_id in withdrawn:
        state["messages"].pop(message_id, None)
    if withdrawn:
        for beliefs in state["beliefs"].values():
            for message_id in withdrawn:
                beliefs.pop(message_id, None)
    state["support_requests"][:] = [
        item for item in state["support_requests"] if item.get("agent_id") != agent_id
    ]
    return withdrawn


def _cancel_agent_events(
    world: WorldState,
    agent_id: str,
    departure: ScheduledEvent,
    withdrawn_message_ids: set[str],
) -> None:
    for pending in world.event_queue.pending():
        if agent_id not in _event_agent_ids(pending):
            continue
        world.event_queue.remove(pending.event_id)
        world.event_log.append(
            EventLogRecord(
                event=pending,
                realized_minute=world.minute_index,
                status=EventStatus.SUPPRESSED,
                detail=f"Agent {agent_id} departed before the event was due",
            )
        )
    for pending in world.event_queue.pending():
        message_id = pending.payload.get("message_id")
        pending_rumor_from_agent = (
            pending.event_type == "information_rumor"
            and pending.payload.get("origin_agent_id") == agent_id
        )
        withdrawn_claim = isinstance(message_id, str) and message_id in withdrawn_message_ids
        if pending_rumor_from_agent or withdrawn_claim:
            world.event_queue.remove(pending.event_id)
            world.event_log.append(
                EventLogRecord(
                    event=pending,
                    realized_minute=world.minute_index,
                    status=EventStatus.SUPPRESSED,
                    detail=f"Claim withdrawn after Agent {agent_id} departed",
                )
            )


def _event_agent_ids(event: ScheduledEvent) -> set[str]:
    return {
        value
        for key, value in event.payload.items()
        if key in {"agent_id", "sender_agent_id", "recipient_agent_id", "participant_agent_id"}
        and isinstance(value, str)
    }


@kernel_first("population", "schedule_population_retry")
def schedule_population_retry(
    world: WorldState,
    event: ScheduledEvent,
    *,
    id_prefix: str = "departure-retry",
    subject: str = "departure",
) -> None:
    """Retry an approved population event next minute, at most 1440 times.

    The retry joins the approved program's scheduled events, so it passes the same
    approval check. The caller then raises EventSuppressed for this minute.
    """
    due = world.minute_index + 1
    if due >= world.duration_minutes:
        raise EventSuppressed(f"{subject} could not complete before the scenario ended")
    retry = event.provenance.get("population_retry", {})
    retry_count = retry.get("count", 0) if isinstance(retry, Mapping) else 0
    if not isinstance(retry_count, int) or retry_count >= 1440:
        raise EventSuppressed(f"{subject} reached the bounded deferral limit")
    sequence = world.allocate_event_sequence()
    digest = hashlib.sha256(f"{event.event_id}:{sequence}".encode()).hexdigest()[:24]
    next_id = f"{id_prefix}-{digest}"
    provenance = {
        **dict(event.provenance),
        "deferred_from": event.event_id,
        "population_retry": {"count": retry_count + 1},
    }
    compiler = provenance.get("effect_program_v1")
    if isinstance(compiler, Mapping):
        program_id = compiler.get("program_id")
        effect_id = compiler.get("effect_id")
        program_state = world.model_state.get("effect_program_v1", {})
        program = program_state.get("programs", {}).get(program_id)
        if isinstance(program, dict) and effect_id in program.get("effect_ids", []):
            program["scheduled_event_ids"].append(next_id)
    world.schedule_event(
        ScheduledEvent(
            event_id=next_id,
            kind=EventKind.ENDOGENOUS,
            due_minute=due,
            priority=event.priority,
            sequence=sequence,
            event_type=event.event_type,
            payload=dict(event.payload),
            source_id=event.source_id,
            causation_id=event.event_id,
            provenance=provenance,
        )
    )


def _state(world: WorldState) -> dict[str, Any]:
    if POPULATION_DYNAMICS_STATE_KEY not in world.model_state:
        world.model_state[POPULATION_DYNAMICS_STATE_KEY] = create_population_dynamics_state(world)
    return world.model_state[POPULATION_DYNAMICS_STATE_KEY]


@kernel_first("population", "zone_has_room")
def _zone_has_room(world: WorldState, zone_id: str) -> bool:
    from sheltersim.domain.temporary_spaces import effective_zone_capacity, incoming_travelers

    capacity = effective_zone_capacity(world, zone_id)
    if capacity is None:
        return True
    occupancy = sum(agent.zone_id == zone_id for agent in world.agents.values())
    # People walking there hold their place, as travel admission counts them (#180).
    return occupancy + incoming_travelers(world, zone_id) < capacity


@kernel_first("population", "choose_admission_target")
def _choose_admission_target(
    world: WorldState, item: dict[str, Any]
) -> tuple[str, str | None] | None:
    """Choose the preferred zone first, then explicitly allowed alternate zones."""
    from sheltersim.domain.geometry_overlay import available_temporary_room

    zone_ids = [item["zone_id"], *item.get("admission_zone_ids", ())]
    for zone_id in zone_ids:
        if _zone_has_room(world, zone_id):
            return zone_id, None
        room_id = available_temporary_room(world, zone_id)
        if room_id is not None:
            return zone_id, room_id
    return None


def _arrival_minute(state: dict[str, Any], agent_id: str) -> int:
    return next(
        (
            record.get("admitted_minute", record.get("created_minute", 0))
            for record in state["arrivals"]
            if record.get("agent_id") == agent_id
        ),
        0,
    )


def _profile_record(profile: AgentProfile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "group_id": profile.group_id,
        "initial_zone_id": profile.initial_zone_id,
        "mobility": profile.mobility.value,
        "support_need": profile.support_need,
        "stress_sensitivity": profile.stress_sensitivity,
        "rng_key": profile.rng_key,
        "initial_needs": {
            "toilet_urgency": profile.initial_needs.toilet_urgency,
            "hunger": profile.initial_needs.hunger,
            "fatigue": profile.initial_needs.fatigue,
            "hydration_need": profile.initial_needs.hydration_need,
        },
        "provenance": profile.provenance,
    }
