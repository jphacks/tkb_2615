"""Same-identity population lifecycle commands and retained absent-person state.

The common presence ledger records where a person is in the population lifecycle;
this module owns the simulation state needed to resume that same identity. Commands
are typed and deterministic. Natural-language compilers must map into these commands
only after validating an approved EffectProgram.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal

from pydantic import Field, model_validator

from sheltersim.contracts.extensions import ExtensionModel, Identifier
from sheltersim.contracts.presence import (
    PRESENCE_NAMESPACE,
    PresenceRecord,
    PresenceState,
    PresenceTransition,
)
from sheltersim.core.world import WorldState
from sheltersim.domain.agent_dynamics import AGENT_DYNAMICS_STATE_KEY
from sheltersim.domain.body_state import BODY_STATE_KEY
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
from sheltersim.domain.models import (
    Activity,
    AgentProfile,
    AgentState,
    InitialNeedProfile,
    Mobility,
    NeedState,
)
from sheltersim.domain.movement import (
    MOVEMENT_STATE_KEY,
    finish_movement,
    route_is_physically_clear,
    route_with_movement_time,
    shared_passage_edge_loads,
    start_movement,
)
from sheltersim.domain.social_network import SOCIAL_NETWORK_STATE_KEY
from sheltersim.events.models import EventApplicationError, EventSuppressed, ScheduledEvent

POPULATION_LIFECYCLE_STATE_KEY = "population_lifecycle_v1"
POPULATION_LIFECYCLE_POLICY_VERSION = "same-id-population-lifecycle-v1"
POPULATION_LIFECYCLE_SOURCE_ID = "ASM-POPULATION-LIFECYCLE-V1"
MAX_LIFECYCLE_HISTORY = 20_000
MAX_RETAINED_PEOPLE = 10_000
MAX_RELATIONSHIP_ARCHIVE = 200_000


class PopulationPresenceCommand(ExtensionModel):
    """Validated controller command; does not accept narrative or executable text."""

    action: Literal["temporary_exit", "return", "transfer", "relocate", "reunite"]
    agent_id: Identifier | None = None
    agent_ids: tuple[Identifier, ...] = Field(default=(), max_length=1000)
    zone_id: Identifier | None = None
    household_id: Identifier | None = None
    expected_return_minute: int | None = Field(default=None, strict=True, ge=0)
    source_ids: tuple[Identifier, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def inputs_match_action(self) -> PopulationPresenceCommand:
        if self.action == "relocate" and self.zone_id is None:
            raise ValueError("relocate requires a destination zone")
        if self.action == "reunite" and self.household_id is None:
            raise ValueError("reunite requires a household ID")
        if self.expected_return_minute is not None and self.action != "temporary_exit":
            raise ValueError("expected_return_minute is valid only for temporary exits")
        return self

    def selected_ids(self) -> tuple[str, ...]:
        ids = tuple(sorted(set(self.agent_ids + ((self.agent_id,) if self.agent_id else ()))))
        if not ids:
            raise ValueError("population command requires at least one agent id")
        if self.action in {"temporary_exit", "return", "transfer", "relocate"} and len(ids) != 1:
            raise ValueError(f"{self.action} requires exactly one agent id")
        if self.action == "reunite" and len(ids) < 2:
            raise ValueError("reunite requires at least two agent ids")
        return ids


def create_population_lifecycle_state() -> dict[str, Any]:
    """Create checkpoint-safe state for temporary absences and deferred ties."""
    return {
        "schema_version": 1,
        "policy_version": POPULATION_LIFECYCLE_POLICY_VERSION,
        "source_id": POPULATION_LIFECYCLE_SOURCE_ID,
        "people": {},
        "relationship_archive": [],
        "messages": {},
        "deferred_beliefs": {},
        "pending_returns": [],
        "pending_relocations": {},
        "history": [],
    }


def initialize_population_lifecycle_state(world: WorldState) -> dict[str, Any]:
    """Install the owned state namespace before the initial checkpoint is captured."""
    state = world.model_state.setdefault(
        POPULATION_LIFECYCLE_STATE_KEY, create_population_lifecycle_state()
    )
    validate_population_lifecycle_state(world)
    return state


def validate_population_lifecycle_state(world: WorldState) -> None:
    """Validate exact schema, archived identities, pending admission, and bounded JSON."""
    state = world.model_state.get(POPULATION_LIFECYCLE_STATE_KEY)
    if state is None:
        return
    fields = {
        "schema_version",
        "policy_version",
        "source_id",
        "people",
        "relationship_archive",
        "messages",
        "deferred_beliefs",
        "pending_returns",
        "pending_relocations",
        "history",
    }
    if (
        not isinstance(state, dict)
        or set(state) != fields
        or state.get("schema_version") != 1
        or state.get("policy_version") != POPULATION_LIFECYCLE_POLICY_VERSION
        or state.get("source_id") != POPULATION_LIFECYCLE_SOURCE_ID
        or not isinstance(state.get("people"), dict)
        or len(state["people"]) > MAX_RETAINED_PEOPLE
        or not isinstance(state.get("relationship_archive"), list)
        or len(state["relationship_archive"]) > MAX_RELATIONSHIP_ARCHIVE
        or not isinstance(state.get("messages"), dict)
        or not isinstance(state.get("deferred_beliefs"), dict)
        or not isinstance(state.get("pending_returns"), list)
        or not isinstance(state.get("pending_relocations"), dict)
        or not isinstance(state.get("history"), list)
        or len(state["history"]) > MAX_LIFECYCLE_HISTORY
    ):
        raise ValueError("population_lifecycle_v1 has an invalid exact schema")
    active_ids = set(world.agents)
    pending_ids: set[str] = set()
    for agent_id, record in state["people"].items():
        if (
            not isinstance(agent_id, str)
            or agent_id in active_ids
            or not isinstance(record, dict)
            or set(record)
            != {
                "agent",
                "agent_dynamics",
                "body_agent",
                "information",
                "since_minute",
                "source_event_ids",
            }
            or type(record["since_minute"]) is not int
            or record["since_minute"] < 0
            or not isinstance(record["source_event_ids"], list)
            or not isinstance(record["information"], dict)
            or set(record["information"]) != {"beliefs", "query_pressure"}
            or not isinstance(record["information"]["beliefs"], dict)
        ):
            raise ValueError("population_lifecycle_v1 archived person is invalid")
        _validate_agent_snapshot(record["agent"], expected_id=agent_id)
        _validate_json(record)
    for item in state["pending_returns"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {"agent_id", "zone_id", "event_id", "minute", "household_id", "source_ids"}
            or item["agent_id"] not in state["people"]
            or item["agent_id"] in active_ids
            or item["zone_id"] not in world.zones
            or item["agent_id"] in pending_ids
            or any(type(item[key]) is not int or item[key] < 0 for key in ("minute",))
            or not isinstance(item["event_id"], str)
            or not isinstance(item["source_ids"], list)
        ):
            raise ValueError("population_lifecycle_v1 pending return is invalid")
        pending_ids.add(item["agent_id"])
    for agent_id, relocation in state["pending_relocations"].items():
        if (
            agent_id not in active_ids
            or not isinstance(relocation, dict)
            or set(relocation)
            != {
                "task_id",
                "event_id",
                "from_zone_id",
                "to_zone_id",
                "household_id",
                "source_ids",
                "started_minute",
                "due_minute",
            }
            or relocation["from_zone_id"] not in world.zones
            or relocation["to_zone_id"] not in world.zones
            or type(relocation["started_minute"]) is not int
            or type(relocation["due_minute"]) is not int
            or relocation["due_minute"] <= relocation["started_minute"]
            or not isinstance(relocation["source_ids"], list)
        ):
            raise ValueError("population_lifecycle_v1 pending relocation is invalid")
    for item in state["relationship_archive"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {"namespace", "pair_key", "agent_ids", "value", "restore_when_both_active"}
            or item["namespace"]
            not in {
                BODY_STATE_KEY,
                SOCIAL_NETWORK_STATE_KEY,
                INFORMATION_SPREAD_STATE_KEY,
                "social_propagation_v1",
            }
            or not isinstance(item["pair_key"], str)
            or not isinstance(item["agent_ids"], list)
            or len(item["agent_ids"]) != 2
            or item["agent_ids"][0] == item["agent_ids"][1]
            or type(item["restore_when_both_active"]) is not bool
        ):
            raise ValueError("population_lifecycle_v1 relationship archive is invalid")
        _validate_json(item["value"])
    for agent_id, relocation in state["pending_relocations"].items():
        matching_tasks = [
            task for task in world.active_tasks.values() if task.id == relocation["task_id"]
        ]
        matching_events = [
            event
            for event in world.event_queue.pending()
            if event.event_id == relocation["event_id"]
        ]
        if (
            len(matching_tasks) != 1
            or matching_tasks[0].agent_id != agent_id
            or matching_tasks[0].kind != "travel_return"
            or matching_tasks[0].due_minute != relocation["due_minute"]
            or len(matching_events) != 1
            or matching_events[0].event_type != "agent_travel_complete"
            or matching_events[0].payload.get("purpose") != "presence_relocation"
        ):
            raise ValueError("population_lifecycle_v1 relocation task/event is inconsistent")
    for message_id, message in state["messages"].items():
        if (
            not isinstance(message_id, str)
            or not isinstance(message, dict)
            or message.get("id") != message_id
        ):
            raise ValueError("population_lifecycle_v1 retained message is invalid")
        _validate_json(message)
    for agent_id, beliefs in state["deferred_beliefs"].items():
        if not isinstance(agent_id, str) or not isinstance(beliefs, dict):
            raise ValueError("population_lifecycle_v1 deferred beliefs are invalid")
        _validate_json(beliefs)
    if PRESENCE_NAMESPACE in world.model_state:
        presence = PresenceState.model_validate(world.model_state[PRESENCE_NAMESPACE])
        for agent_id in pending_ids:
            record = presence.people.get(agent_id)
            if record is None or record.status != "awaiting_admission":
                raise ValueError("pending return is inconsistent with population_presence_v1")


def apply_population_presence_command(
    world: WorldState,
    event: ScheduledEvent,
    command: PopulationPresenceCommand | dict[str, Any],
) -> dict[str, Any]:
    """Apply one typed lifecycle command atomically and return its recorded receipts.

    The caller is the approved event/effect controller. An LLM must never call this
    directly; narrative instructions first pass through the typed EffectProgram.
    """
    from sheltersim.core.world import WorldState as MutableWorldState
    from sheltersim.domain.simulator_state import validate_simulator_extensions

    try:
        typed = (
            command
            if isinstance(command, PopulationPresenceCommand)
            else PopulationPresenceCommand.model_validate(command)
        )
        ids = typed.selected_ids()
    except (TypeError, ValueError) as exc:
        raise EventApplicationError(f"population command is invalid: {exc}") from exc
    if POPULATION_LIFECYCLE_STATE_KEY not in world.model_state:
        raise EventApplicationError("population lifecycle contract is not installed")
    candidate = MutableWorldState.from_dict(deepcopy(world.to_dict()))
    state = candidate.model_state[POPULATION_LIFECYCLE_STATE_KEY]
    minute = event.due_minute
    if minute != candidate.minute_index:
        raise EventApplicationError(
            "population command minute must match the current event boundary"
        )
    if typed.expected_return_minute is not None and typed.expected_return_minute < minute:
        if "population_retry" in event.provenance:
            # Deferred while busy (#181) past the time it was meant to end.
            raise EventSuppressed(
                "temporary exit dropped: the person was not free before the expected return"
            )
        raise EventApplicationError("expected return minute precedes the departure event")
    if typed.action == "temporary_exit":
        agent_id = ids[0]
        agent = candidate.agents.get(agent_id)
        if agent is None:
            raise EventApplicationError("temporary exit requires an admitted person")
        returns = _program_presence_minutes(world, event, agent_id, "return")
        if any(due <= minute for due in returns):
            # Deferred while busy (#181) until the return of the same program (#185).
            raise EventSuppressed(
                "temporary exit dropped: the person was not free before the return in the "
                "same EffectProgram"
            )
        _defer_while_busy(world, event, agent_id)
        _archive_person(candidate, agent_id, event, state)
        presence = _presence_state(candidate)
        if presence is not None:
            _set_presence(
                presence,
                event,
                agent_id,
                "temporarily_absent",
                minute,
                from_zone=agent.zone_id,
                return_zone=agent.zone_id,
                expected_return=typed.expected_return_minute,
                household_id=typed.household_id,
                source_ids=typed.source_ids,
            )
        receipt = _history(
            candidate, state, event, agent_id, "temporary_exit", agent.zone_id, None, typed
        )
    elif typed.action == "transfer":
        agent_id = ids[0]
        agent = candidate.agents.get(agent_id)
        if agent is None:
            raise EventApplicationError("transfer requires an admitted person")
        _defer_while_busy(world, event, agent_id)
        from_zone = agent.zone_id
        _archive_person(candidate, agent_id, event, state)
        presence = _presence_state(candidate)
        if presence is not None:
            _set_presence(
                presence,
                event,
                agent_id,
                "transferred",
                minute,
                from_zone=from_zone,
                return_zone=typed.zone_id,
                expected_return=typed.expected_return_minute,
                household_id=typed.household_id,
                source_ids=typed.source_ids,
            )
        receipt = _history(
            candidate, state, event, agent_id, "transferred", from_zone, typed.zone_id, typed
        )
    elif typed.action == "return":
        agent_id = ids[0]
        if agent_id in candidate.agents and any(
            due < minute
            for due in _program_presence_minutes(world, event, agent_id, "temporary_exit")
        ):
            # The exit of the same program was deferred past this return and is dropped.
            raise EventSuppressed(
                "return skipped: the person had not left before the return in the same "
                "EffectProgram"
            )
        if agent_id in candidate.agents or agent_id not in state["people"]:
            raise EventApplicationError("return requires a retained absent person")
        presence = _presence_state(candidate)
        presence_person = (
            PresenceRecord.model_validate(presence["people"][agent_id])
            if presence is not None and agent_id in presence["people"]
            else None
        )
        destination = typed.zone_id or (presence_person.return_zone_id if presence_person else None)
        if destination not in candidate.zones:
            raise EventApplicationError("return requires a valid destination zone")
        household_id = (
            typed.household_id
            if typed.household_id is not None
            else (presence_person.household_id if presence_person else None)
        )
        from sheltersim.domain.geometry_overlay import available_temporary_room

        room_id = (
            None
            if _zone_has_room(candidate, destination)
            else available_temporary_room(candidate, destination)
        )
        if not _zone_has_room(candidate, destination) and room_id is None:
            pending = {
                "agent_id": agent_id,
                "zone_id": destination,
                "event_id": event.event_id,
                "minute": minute,
                "household_id": household_id,
                "source_ids": sorted(set(typed.source_ids + (event.source_id,))),
            }
            state["pending_returns"] = [
                item for item in state["pending_returns"] if item["agent_id"] != agent_id
            ]
            state["pending_returns"].append(pending)
            if presence is not None:
                _set_presence(
                    presence,
                    event,
                    agent_id,
                    "awaiting_admission",
                    minute,
                    from_zone=presence_person.transition_zone_id if presence_person else None,
                    return_zone=destination,
                    household_id=household_id,
                    source_ids=typed.source_ids,
                )
            receipt = _history(
                candidate, state, event, agent_id, "waiting", None, destination, typed
            )
        else:
            _restore_person(
                candidate,
                agent_id,
                destination,
                household_id,
                minute,
                state,
                room_id=room_id,
                event_id=event.event_id,
            )
            if presence is not None:
                _set_presence(
                    presence,
                    event,
                    agent_id,
                    "admitted",
                    minute,
                    to_zone=destination,
                    household_id=household_id,
                    source_ids=typed.source_ids,
                )
            receipt = _history(
                candidate, state, event, agent_id, "returned", None, destination, typed
            )
    elif typed.action == "relocate":
        agent_id = ids[0]
        agent = candidate.agents.get(agent_id)
        if agent is None or typed.zone_id not in candidate.zones:
            raise EventApplicationError(
                "relocation requires an admitted person and valid destination"
            )
        if agent.activity not in (Activity.IDLE, Activity.RESTING, Activity.SLEEPING):
            raise EventApplicationError("relocation requires a person who is not in an active task")
        if not _zone_has_room(candidate, typed.zone_id, excluding=agent_id):
            raise EventApplicationError("relocation destination has no available capacity")
        from_zone = agent.zone_id
        receipt = _schedule_relocation(candidate, event, typed, agent_id, from_zone, state)
    else:
        if typed.household_id is None:
            raise EventApplicationError("reunion requires a target household id")
        agents = [candidate.agents.get(agent_id) for agent_id in ids]
        if any(agent is None for agent in agents):
            raise EventApplicationError("reunion requires every person to be admitted")
        zones = {agent.zone_id for agent in agents if agent is not None}
        if len(zones) != 1:
            raise EventApplicationError("reunion requires people to be physically co-located")
        presence = _presence_state(candidate)
        if presence is None:
            raise EventApplicationError("reunion requires the population presence contract")
        for agent_id in ids:
            _set_presence(
                presence,
                event,
                agent_id,
                "admitted",
                minute,
                from_zone=next(iter(zones)),
                to_zone=next(iter(zones)),
                household_id=typed.household_id,
                source_ids=typed.source_ids,
                action="reunited",
            )
            _history(
                candidate,
                state,
                event,
                agent_id,
                "reunited",
                next(iter(zones), None),
                next(iter(zones), None),
                typed,
            )
        receipt = {
            "event_id": event.event_id,
            "agent_ids": list(ids),
            "household_id": typed.household_id,
            "minute": minute,
            "action": "reunited",
        }
    try:
        validate_population_lifecycle_state(candidate)
        candidate.validate()
        validate_simulator_extensions(candidate)
    except (TypeError, KeyError, ValueError) as exc:
        raise EventApplicationError(f"population command failed validation: {exc}") from exc
    world.__dict__.clear()
    world.__dict__.update(candidate.__dict__)
    return receipt


def admit_pending_returns(world: WorldState, minute: int) -> tuple[str, ...]:
    """Admit same-ID returnees in stable request order when destination capacity opens."""
    state = world.model_state.get(POPULATION_LIFECYCLE_STATE_KEY)
    if not isinstance(state, dict) or not state["pending_returns"]:
        return ()
    admitted: list[str] = []
    remaining = []
    presence = _presence_state(world)
    for pending in sorted(
        state["pending_returns"],
        key=lambda item: (item["minute"], item["event_id"], item["agent_id"]),
    ):
        from sheltersim.domain.geometry_overlay import available_temporary_room

        room_id = (
            None
            if _zone_has_room(world, pending["zone_id"])
            else available_temporary_room(world, pending["zone_id"])
        )
        if not _zone_has_room(world, pending["zone_id"]) and room_id is None:
            remaining.append(pending)
            continue
        _restore_person(
            world,
            pending["agent_id"],
            pending["zone_id"],
            pending["household_id"],
            minute,
            state,
            room_id=room_id,
            event_id=pending["event_id"],
        )
        if presence is not None:
            synthetic = _presence_event(pending)
            _set_presence(
                presence,
                synthetic,
                pending["agent_id"],
                "admitted",
                minute,
                to_zone=pending["zone_id"],
                household_id=pending["household_id"],
                source_ids=tuple(pending["source_ids"]),
                action="returned",
            )
        _append_history(
            state,
            {
                "event_id": pending["event_id"],
                "agent_id": pending["agent_id"],
                "action": "admitted",
                "minute": minute,
                "from_zone_id": None,
                "to_zone_id": pending["zone_id"],
                "household_id": pending["household_id"],
                "source_ids": list(pending["source_ids"]),
            },
        )
        admitted.append(pending["agent_id"])
    state["pending_returns"] = remaining
    _restore_ready_relationships(world, state)
    validate_population_lifecycle_state(world)
    return tuple(admitted)


def complete_presence_relocation(world: WorldState, event: ScheduledEvent) -> None:
    """Commit household/location receipts after the existing movement handler arrives."""
    state = world.model_state.get(POPULATION_LIFECYCLE_STATE_KEY)
    agent_id = event.payload.get("agent_id")
    relocation = (
        state.get("pending_relocations", {}).get(agent_id) if isinstance(state, dict) else None
    )
    if (
        not isinstance(relocation, dict)
        or relocation["event_id"] != event.event_id
        or relocation["due_minute"] != event.due_minute
        or world.agents.get(agent_id) is None
        or world.agents[agent_id].zone_id != relocation["to_zone_id"]
    ):
        raise EventApplicationError("relocation completion does not match its pending route")
    presence = _presence_state(world)
    if presence is not None:
        _set_presence(
            presence,
            event,
            agent_id,
            "admitted",
            event.due_minute,
            from_zone=relocation["from_zone_id"],
            to_zone=relocation["to_zone_id"],
            household_id=relocation["household_id"],
            source_ids=tuple(relocation["source_ids"]),
            action="relocated",
        )
    _append_history(
        state,
        {
            "event_id": event.event_id,
            "agent_id": agent_id,
            "action": "relocated",
            "minute": event.due_minute,
            "from_zone_id": relocation["from_zone_id"],
            "to_zone_id": relocation["to_zone_id"],
            "household_id": relocation["household_id"],
            "source_ids": list(relocation["source_ids"]),
        },
    )
    state["pending_relocations"].pop(agent_id)
    admit_pending_returns(world, event.due_minute)
    validate_population_lifecycle_state(world)


def current_household_id(world: WorldState, agent_id: str) -> str | None:
    """Resolve the mutable household ledger without rewriting immutable profile traits."""
    presence = world.model_state.get(PRESENCE_NAMESPACE)
    if isinstance(presence, dict):
        person = presence.get("people", {}).get(agent_id, {})
        if isinstance(person, dict):
            return person.get("household_id")
    agent = world.agents.get(agent_id)
    return agent.profile.group_id if agent is not None else None


def _archive_person(
    world: WorldState, agent_id: str, event: ScheduledEvent, state: dict[str, Any]
) -> None:
    if agent_id in state["people"]:
        raise EventApplicationError("person already has a retained lifecycle archive")
    agent = world.agents[agent_id]
    from sheltersim.domain.geometry_overlay import (
        release_sleep_bed,
        release_temporary_room_admission,
    )

    release_sleep_bed(world, agent_id)
    release_temporary_room_admission(world, agent_id)
    info = world.model_state.get(INFORMATION_SPREAD_STATE_KEY)
    information = {"beliefs": {}, "query_pressure": None}
    if isinstance(info, dict):
        information["beliefs"] = deepcopy(info["beliefs"].pop(agent_id, {}))
        information["query_pressure"] = info["query_pressure"].pop(agent_id, 0.0)
        for key in sorted(tuple(info["contacts"])):
            pair = _pair_from_key(key)
            if agent_id in pair:
                _archive_relationship(
                    state, INFORMATION_SPREAD_STATE_KEY, key, pair, info["contacts"].pop(key), True
                )
        for message_id in sorted(tuple(info["messages"])):
            message = info["messages"][message_id]
            if message.get("origin_agent_id") == agent_id:
                state["messages"][message_id] = info["messages"].pop(message_id)
                for other_id, beliefs in info["beliefs"].items():
                    belief = beliefs.pop(message_id, None)
                    if belief is not None:
                        state["deferred_beliefs"].setdefault(other_id, {})[message_id] = belief
                for archived_id, archive in state["people"].items():
                    belief = archive["information"]["beliefs"].pop(message_id, None)
                    if belief is not None:
                        state["deferred_beliefs"].setdefault(archived_id, {})[message_id] = belief
        info["support_requests"][:] = [
            item for item in info["support_requests"] if item.get("agent_id") != agent_id
        ]
    withdrawn_message_ids = {
        message_id
        for message_id, message in state["messages"].items()
        if message.get("origin_agent_id") == agent_id
    }
    body = world.model_state.get(BODY_STATE_KEY)
    body_agent = None
    if isinstance(body, dict):
        body_agent = deepcopy(body["agents"].pop(agent_id, None))
        for key in sorted(tuple(body["relationships"])):
            pair = _pair_from_key(key)
            if agent_id in pair:
                _archive_relationship(
                    state, BODY_STATE_KEY, key, pair, body["relationships"].pop(key), True
                )
    dynamics = world.model_state.get(AGENT_DYNAMICS_STATE_KEY)
    dynamics_record = dynamics["agents"].pop(agent_id, None) if isinstance(dynamics, dict) else None
    social = world.model_state.get(SOCIAL_NETWORK_STATE_KEY)
    if isinstance(social, dict):
        for key in sorted(tuple(social["relationships"])):
            pair = _pair_from_key(key)
            if agent_id in pair:
                _archive_relationship(
                    state,
                    SOCIAL_NETWORK_STATE_KEY,
                    key,
                    pair,
                    social["relationships"].pop(key),
                    True,
                )
    social_runtime = world.model_state.get("social_propagation_v1")
    if isinstance(social_runtime, dict):
        for key in sorted(tuple(social_runtime.get("contacts", {}))):
            pair = _pair_from_key(key)
            if agent_id in pair:
                _archive_relationship(
                    state,
                    "social_propagation_v1",
                    key,
                    pair,
                    social_runtime["contacts"].pop(key),
                    True,
                )
        social_runtime.get("agent_cooldown_until", {}).pop(agent_id, None)
    snapshot = {
        "agent": _agent_record(agent),
        "agent_dynamics": dynamics_record,
        "body_agent": body_agent,
        "information": information,
        "since_minute": event.due_minute,
        "source_event_ids": sorted(
            set([event.event_id, *([event.causation_id] if event.causation_id else [])])
        ),
    }
    _validate_agent_snapshot(snapshot["agent"], expected_id=agent_id)
    state["people"][agent_id] = snapshot
    from sheltersim.domain.population import _cancel_agent_events

    _cancel_agent_events(world, agent_id, event, withdrawn_message_ids)
    world.support_requests = {
        key: value for key, value in world.support_requests.items() if value.agent_id != agent_id
    }
    if MOVEMENT_STATE_KEY in world.model_state:
        world.model_state[MOVEMENT_STATE_KEY]["journeys"].pop(agent_id, None)
        world.model_state[MOVEMENT_STATE_KEY]["positions"].pop(agent_id, None)
    world.agents.pop(agent_id)


def _restore_person(
    world: WorldState,
    agent_id: str,
    zone_id: str,
    household_id: str | None,
    minute: int,
    state: dict[str, Any],
    *,
    room_id: str | None = None,
    event_id: str | None = None,
) -> None:
    snapshot = state["people"].get(agent_id)
    if not isinstance(snapshot, dict):
        raise EventApplicationError("retained person state is missing")
    raw = snapshot["agent"]
    profile_raw = raw["profile"]
    initial = dict(profile_raw["initial_needs"])
    if world.schema_version == 5:
        initial["hydration_need"] = 0.0
    profile = AgentProfile(
        id=profile_raw["id"],
        group_id=profile_raw["group_id"],
        initial_zone_id=profile_raw["initial_zone_id"],
        mobility=Mobility(profile_raw["mobility"]),
        support_need=profile_raw["support_need"],
        stress_sensitivity=profile_raw["stress_sensitivity"],
        rng_key=profile_raw["rng_key"],
        initial_needs=InitialNeedProfile(**initial),
        provenance=dict(profile_raw["provenance"]),
    )
    needs = dict(raw["needs"])
    if world.schema_version == 5:
        needs["hydration_need"] = 0.0
    agent = AgentState(
        profile=profile,
        zone_id=zone_id,
        activity=Activity.IDLE,
        target_id=None,
        needs=NeedState(**needs),
        stress=raw["stress"],
        trust=raw["trust"],
        next_decision_at=minute,
        support_request_id=None,
        support_cooldown_until=raw["support_cooldown_until"],
    )
    world.agents[agent_id] = agent
    if AGENT_DYNAMICS_STATE_KEY in world.model_state:
        record = snapshot["agent_dynamics"]
        world.model_state[AGENT_DYNAMICS_STATE_KEY]["agents"][agent_id] = (
            deepcopy(record) if record is not None else _initial_dynamics(world, agent_id)
        )
        world.model_state[AGENT_DYNAMICS_STATE_KEY]["agents"][agent_id]["decision_due_minute"] = (
            minute
        )
    if BODY_STATE_KEY in world.model_state:
        record = snapshot["body_agent"]
        if record is not None:
            world.model_state[BODY_STATE_KEY]["agents"][agent_id] = deepcopy(record)
    if INFORMATION_SPREAD_STATE_KEY in world.model_state:
        info = world.model_state[INFORMATION_SPREAD_STATE_KEY]
        deferred = state["deferred_beliefs"].setdefault(agent_id, {})
        info["beliefs"][agent_id] = {
            message_id: deepcopy(belief)
            for message_id, belief in snapshot["information"]["beliefs"].items()
            if message_id in info["messages"]
        }
        for message_id, belief in snapshot["information"]["beliefs"].items():
            if message_id not in info["messages"]:
                deferred[message_id] = deepcopy(belief)
        if not deferred:
            state["deferred_beliefs"].pop(agent_id, None)
        info["query_pressure"][agent_id] = snapshot["information"]["query_pressure"] or 0.0
        for message_id, message in tuple(state["messages"].items()):
            if message.get("origin_agent_id") == agent_id:
                info["messages"][message_id] = state["messages"].pop(message_id)
    _reset_position(world, agent_id)
    if room_id is not None:
        if event_id is None:
            raise EventApplicationError("room admission on return requires its arrival event ID")
        from sheltersim.domain.geometry_overlay import admit_to_temporary_room

        point = admit_to_temporary_room(
            world,
            agent_id=agent_id,
            room_id=room_id,
            minute=minute,
            event_id=event_id,
        )
        movement = world.model_state.get(MOVEMENT_STATE_KEY, {})
        position = (
            movement.get("positions", {}).get(agent_id) if isinstance(movement, dict) else None
        )
        if isinstance(position, dict):
            position.update(
                {
                    "zone_id": zone_id,
                    "point": point.model_dump(mode="json"),
                    "edge_id": None,
                    "progress": 1.0,
                    "position_kind": "physical",
                }
            )
    _restore_ready_relationships(world, state)
    _restore_deferred_beliefs(world, state)
    state["people"].pop(agent_id)
    state["pending_returns"] = [
        item for item in state["pending_returns"] if item["agent_id"] != agent_id
    ]


def _schedule_relocation(
    world: WorldState,
    event: ScheduledEvent,
    command: PopulationPresenceCommand,
    agent_id: str,
    from_zone: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    destination = command.zone_id
    if destination is None:
        raise EventApplicationError("relocation destination is required")
    if destination == from_zone:
        agent = world.agents[agent_id]
        agent.next_decision_at = event.due_minute + 1
        presence = _presence_state(world)
        if presence is not None:
            _set_presence(
                presence,
                event,
                agent_id,
                "admitted",
                event.due_minute,
                from_zone=from_zone,
                to_zone=destination,
                household_id=command.household_id,
                source_ids=command.source_ids,
                action="relocated",
            )
        return _history(world, state, event, agent_id, "relocated", from_zone, destination, command)
    from sheltersim.domain.zones import ZoneGraph
    from sheltersim.policies.space import SpaceRouter

    route_config = world.model_configs.get("layout_routing", {})
    route = SpaceRouter(
        ZoneGraph.from_states(
            world.zones,
            world.zone_edges,
            edge_capacities=route_config.get("max_agents_in_transit_by_edge", {}),
        )
    ).route(
        from_zone,
        destination,
        agent_id,
        edge_loads=shared_passage_edge_loads(world, event.due_minute),
    )
    if route is None or any(
        not route_is_physically_clear(
            world, edge_id, mobility=world.agents[agent_id].profile.mobility.value
        )
        for edge_id in route.edge_ids
    ):
        raise EventApplicationError("no accessible, physically clear relocation route is available")
    route = route_with_movement_time(world, agent_id, route)
    due_minute = event.due_minute + route.travel_minutes
    if due_minute >= world.duration_minutes:
        raise EventApplicationError("relocation route would finish after the scenario boundary")
    from sheltersim.domain.geometry_overlay import (
        release_sleep_bed,
        release_temporary_room_admission,
    )

    release_sleep_bed(world, agent_id)
    release_temporary_room_admission(world, agent_id)
    sequence = world.allocate_event_sequence()
    task_id = f"presence-relocation-{sequence}"
    event_id = f"presence-relocation-arrival-{sequence}"
    from sheltersim.events.models import EventKind

    route_event = ScheduledEvent(
        event_id=event_id,
        kind=EventKind.COMPLETION,
        due_minute=due_minute,
        priority=event.priority,
        sequence=sequence,
        event_type="agent_travel_complete",
        payload={
            "agent_id": agent_id,
            "task_id": task_id,
            "purpose": "presence_relocation",
            "destination_zone_id": destination,
            "origin_zone_id": from_zone,
            "route_zone_ids": list(route.zone_ids),
            "route_edge_ids": list(route.edge_ids),
            "travel_minutes": route.travel_minutes,
        },
        source_id=POPULATION_LIFECYCLE_SOURCE_ID,
        causation_id=event.event_id,
        provenance={
            "source_type": "assumption",
            "source_id": POPULATION_LIFECYCLE_SOURCE_ID,
            "policy_version": POPULATION_LIFECYCLE_POLICY_VERSION,
        },
    )
    from sheltersim.domain.models import ActiveTask

    world.active_tasks[task_id] = ActiveTask(
        id=task_id,
        kind="travel_return",
        agent_id=agent_id,
        resource_id=None,
        start_minute=event.due_minute,
        due_minute=due_minute,
    )
    agent = world.agents[agent_id]
    agent.activity = Activity.MOVING
    agent.target_id = destination
    world.schedule_event(route_event)
    if MOVEMENT_STATE_KEY in world.model_state:
        start_movement(
            world,
            agent_id=agent_id,
            route=route,
            purpose="presence_relocation",
            facility_id=None,
            started_minute=event.due_minute,
            due_minute=due_minute,
            event_id=event_id,
            task_id=task_id,
        )
    state["pending_relocations"][agent_id] = {
        "task_id": task_id,
        "event_id": event_id,
        "from_zone_id": from_zone,
        "to_zone_id": destination,
        "household_id": command.household_id,
        "source_ids": sorted(set(command.source_ids + (event.source_id,))),
        "started_minute": event.due_minute,
        "due_minute": due_minute,
    }
    return {
        "event_id": event.event_id,
        "agent_id": agent_id,
        "action": "relocation_scheduled",
        "minute": event.due_minute,
        "from_zone_id": from_zone,
        "to_zone_id": destination,
        "arrival_minute": due_minute,
        "task_id": task_id,
        "source_ids": sorted(set(command.source_ids + (event.source_id,))),
    }


def _restore_ready_relationships(world: WorldState, state: dict[str, Any]) -> None:
    remaining = []
    for item in state["relationship_archive"]:
        a, b = item["agent_ids"]
        if item["restore_when_both_active"] and (a not in world.agents or b not in world.agents):
            remaining.append(item)
            continue
        namespace = item["namespace"]
        target = world.model_state.get(namespace)
        if not isinstance(target, dict):
            remaining.append(item)
            continue
        if namespace == SOCIAL_NETWORK_STATE_KEY:
            collection = target["relationships"]
        elif namespace == BODY_STATE_KEY:
            collection = target["relationships"]
        elif namespace == INFORMATION_SPREAD_STATE_KEY:
            collection = target["contacts"]
        elif namespace == "social_propagation_v1":
            collection = target.get("contacts")
            if not isinstance(collection, dict):
                remaining.append(item)
                continue
        else:
            remaining.append(item)
            continue
        if item["pair_key"] not in collection:
            collection[item["pair_key"]] = deepcopy(item["value"])
    state["relationship_archive"] = remaining


def _restore_deferred_beliefs(world: WorldState, state: dict[str, Any]) -> None:
    info = world.model_state.get(INFORMATION_SPREAD_STATE_KEY)
    if not isinstance(info, dict):
        return
    for agent_id in sorted(tuple(state["deferred_beliefs"])):
        if agent_id not in world.agents:
            continue
        beliefs = state["deferred_beliefs"][agent_id]
        for message_id in sorted(tuple(beliefs)):
            if message_id in info["messages"]:
                info["beliefs"][agent_id][message_id] = beliefs.pop(message_id)
        if not beliefs:
            state["deferred_beliefs"].pop(agent_id)


def _archive_relationship(
    state: dict[str, Any],
    namespace: str,
    key: str,
    pair: tuple[str, str],
    value: dict[str, Any],
    restore_when_both_active: bool,
) -> None:
    if any(
        item["namespace"] == namespace and item["pair_key"] == key
        for item in state["relationship_archive"]
    ):
        return
    state["relationship_archive"].append(
        {
            "namespace": namespace,
            "pair_key": key,
            "agent_ids": list(pair),
            "value": deepcopy(value),
            "restore_when_both_active": restore_when_both_active,
        }
    )


def _set_presence(
    presence: dict[str, Any],
    event: ScheduledEvent,
    agent_id: str,
    status: str,
    minute: int,
    *,
    from_zone: str | None = None,
    to_zone: str | None = None,
    return_zone: str | None = None,
    expected_return: int | None = None,
    household_id: str | None = None,
    source_ids: tuple[str, ...] = (),
    action: str | None = None,
) -> None:
    raw_old = presence["people"].get(agent_id)
    old = PresenceRecord.model_validate(raw_old) if raw_old is not None else None
    current_household = (
        household_id if household_id is not None else (old.household_id if old else None)
    )
    archive_path = (
        (POPULATION_LIFECYCLE_STATE_KEY, "people", agent_id)
        if status in {"temporarily_absent", "transferred", "awaiting_admission"}
        else None
    )
    current_return_zone = (
        None
        if status == "admitted"
        else (return_zone if return_zone is not None else (old.return_zone_id if old else None))
    )
    current_expected_return = (
        None
        if status == "admitted"
        else (
            expected_return
            if expected_return is not None
            else (old.expected_return_minute if old else None)
        )
    )
    record = PresenceRecord(
        agent_id=agent_id,
        household_id=current_household,
        status=status,
        since_minute=minute,
        transition_zone_id=to_zone or from_zone or (old.transition_zone_id if old else None),
        return_zone_id=current_return_zone,
        expected_return_minute=current_expected_return,
        archive_path=archive_path,
        source_event_ids=tuple(
            sorted(set((old.source_event_ids if old else ()) + (event.event_id,) + source_ids))
        ),
    )
    presence["people"][agent_id] = record.model_dump(mode="json")
    transition_action = action or {
        "temporarily_absent": "temporary_exit",
        "transferred": "transferred",
        "awaiting_admission": "waiting",
        "admitted": "admitted",
    }.get(status, "admitted")
    transition_id = _stable_id(event.event_id, agent_id, transition_action)
    snapshot_hash = hashlib.sha256(
        json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    transition = PresenceTransition(
        transition_id=transition_id,
        event_id=event.event_id,
        agent_id=agent_id,
        minute=minute,
        action=transition_action,
        from_zone_id=from_zone,
        to_zone_id=to_zone,
        from_household_id=old.household_id if old else None,
        to_household_id=current_household,
        causation_id=event.causation_id,
        source_ids=tuple(sorted(set(source_ids + (event.source_id,)))),
        state_snapshot_hash=snapshot_hash,
    )
    # In place: a new list would be converted again, record by record, at every transition.
    presence["history"].append(transition.model_dump(mode="json"))
    del presence["history"][:-20_000]


def _presence_state(world: WorldState) -> dict[str, Any] | None:
    raw = world.model_state.get(PRESENCE_NAMESPACE)
    if not isinstance(raw, dict):
        return None
    PresenceState.model_validate(raw)
    return raw


def _history(
    world: WorldState,
    state: dict[str, Any],
    event: ScheduledEvent,
    agent_id: str,
    action: str,
    from_zone: str | None,
    to_zone: str | None,
    command: PopulationPresenceCommand,
) -> dict[str, Any]:
    row = {
        "event_id": event.event_id,
        "agent_id": agent_id,
        "action": action,
        "minute": event.due_minute,
        "from_zone_id": from_zone,
        "to_zone_id": to_zone,
        "household_id": command.household_id or current_household_id(world, agent_id),
        "source_ids": sorted(set(command.source_ids + (event.source_id,))),
    }
    _append_history(state, row)
    return deepcopy(row)


def _append_history(state: dict[str, Any], row: dict[str, Any]) -> None:
    state["history"].append(row)
    if len(state["history"]) > MAX_LIFECYCLE_HISTORY:
        del state["history"][: len(state["history"]) - MAX_LIFECYCLE_HISTORY]


def _program_presence_minutes(
    world: WorldState, event: ScheduledEvent, agent_id: str, action: str
) -> list[int]:
    """Due minutes of the commands with this action for this person that the
    EffectProgram which scheduled event also scheduled (#185)."""
    # Event payloads and provenance are frozen mappings.
    marker = event.provenance.get("effect_program_v1")
    programs = world.model_state.get("effect_program_v1", {}).get("programs", {})
    program = programs.get(marker.get("program_id")) if isinstance(marker, Mapping) else None
    if not isinstance(program, dict):
        return []
    pending = {item.event_id: item for item in world.event_queue.pending()}
    minutes = []
    for event_id in program.get("scheduled_event_ids", []):
        record = None if event_id in pending else world.event_log.find(event_id)
        scheduled = pending.get(event_id) or (record.event if record is not None else None)
        if scheduled is None or scheduled.event_type != "effect_program_population":
            continue
        command = scheduled.payload.get("command")
        if (
            isinstance(command, Mapping)
            and command.get("action") == action
            and command.get("agent_id") == agent_id
        ):
            minutes.append(scheduled.due_minute)
    return minutes


def _defer_while_busy(world: WorldState, event: ScheduledEvent, agent_id: str) -> None:
    """Retry an exit or transfer next minute while the person is busy (#181).

    The person finishes an active task, a queue place or a support request first, as
    an approved departure of residents already does. The retry is scheduled on the
    world itself, because the command's candidate copy is discarded on suppression.
    """
    agent = world.agents[agent_id]
    if agent.activity not in (Activity.IDLE, Activity.RESTING, Activity.SLEEPING):
        reason = f"the person is {agent.activity.value}"
    elif agent.support_request_id is not None:
        reason = "the person is waiting for support"
    elif any(task.agent_id == agent_id for task in world.active_tasks.values()):
        reason = "the person owns an active task"
    else:
        return
    from sheltersim.domain.population import schedule_population_retry

    schedule_population_retry(world, event, id_prefix="presence-retry", subject="population exit")
    raise EventSuppressed(f"population exit waits until the person is free: {reason}")


def _zone_has_room(world: WorldState, zone_id: str, *, excluding: str | None = None) -> bool:
    from sheltersim.domain.temporary_spaces import effective_zone_capacity, incoming_travelers

    occupancy = sum(
        agent_id != excluding and agent.zone_id == zone_id
        for agent_id, agent in world.agents.items()
    )
    total = occupancy + incoming_travelers(world, zone_id, excluding=excluding)
    capacity = effective_zone_capacity(world, zone_id, occupancy=total)
    return capacity is None or total < capacity


def _reset_position(world: WorldState, agent_id: str) -> None:
    state = world.model_state.get(MOVEMENT_STATE_KEY)
    if not isinstance(state, dict):
        return
    state["journeys"].pop(agent_id, None)
    state["positions"][agent_id] = {
        "zone_id": world.agents[agent_id].zone_id,
        "point": None,
        "edge_id": None,
        "progress": 0.0,
        "position_kind": "unavailable",
    }
    finish_movement(world, agent_id)


def _agent_record(agent: AgentState) -> dict[str, Any]:
    profile = agent.profile
    return {
        "profile": {
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
            "provenance": deepcopy(profile.provenance),
        },
        "zone_id": agent.zone_id,
        "activity": agent.activity.value,
        "target_id": agent.target_id,
        "needs": {
            "toilet_urgency": agent.needs.toilet_urgency,
            "hunger": agent.needs.hunger,
            "fatigue": agent.needs.fatigue,
            "hydration_need": agent.needs.hydration_need,
        },
        "stress": agent.stress,
        "trust": agent.trust,
        "next_decision_at": agent.next_decision_at,
        "support_request_id": agent.support_request_id,
        "support_cooldown_until": agent.support_cooldown_until,
    }


def _validate_agent_snapshot(raw: Any, *, expected_id: str) -> None:
    if not isinstance(raw, dict) or set(raw) != {
        "profile",
        "zone_id",
        "activity",
        "target_id",
        "needs",
        "stress",
        "trust",
        "next_decision_at",
        "support_request_id",
        "support_cooldown_until",
    }:
        raise ValueError("population lifecycle Agent snapshot has an invalid schema")
    profile = raw["profile"]
    if not isinstance(profile, dict) or profile.get("id") != expected_id:
        raise ValueError("population lifecycle Agent snapshot identity is invalid")
    profile_fields = {
        "id",
        "group_id",
        "initial_zone_id",
        "mobility",
        "support_need",
        "stress_sensitivity",
        "rng_key",
        "initial_needs",
        "provenance",
    }
    if set(profile) != profile_fields or not isinstance(profile["initial_needs"], dict):
        raise ValueError("population lifecycle profile snapshot is invalid")
    Activity(raw["activity"])
    Mobility(profile["mobility"])
    for value in (
        raw["stress"],
        raw["trust"],
        profile["support_need"],
        profile["stress_sensitivity"],
    ):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise ValueError("population lifecycle normalized Agent value is invalid")
    _validate_json(raw)


def _agent_ids_for_archive(namespace: str, key: str) -> tuple[str, str]:
    del namespace
    return _pair_from_key(key)


def _pair_from_key(key: str) -> tuple[str, str]:
    try:
        pair = json.loads(key)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("relationship pair key is invalid") from exc
    if (
        not isinstance(pair, list)
        or len(pair) != 2
        or any(not isinstance(item, str) for item in pair)
    ):
        raise ValueError("relationship pair key must contain two Agent IDs")
    return pair[0], pair[1]


def _initial_dynamics(world: WorldState, agent_id: str) -> dict[str, Any]:
    from sheltersim.domain.agent_dynamics import create_agent_dynamics_state

    return create_agent_dynamics_state(world)["agents"][agent_id]


def _presence_event(record: dict[str, Any]) -> ScheduledEvent:
    from sheltersim.events.models import EventKind

    return ScheduledEvent(
        event_id=record["event_id"],
        kind=EventKind.ENDOGENOUS,
        due_minute=record["minute"],
        priority=0,
        sequence=0,
        event_type="population_return_admission",
        payload={"agent_id": record["agent_id"]},
        source_id=POPULATION_LIFECYCLE_SOURCE_ID,
    )


def _stable_id(event_id: str, agent_id: str, action: str) -> str:
    digest = hashlib.sha256(f"{event_id}\0{agent_id}\0{action}".encode()).hexdigest()[:24]
    return f"presence-{digest}"


def _validate_json(value: Any) -> None:
    json.dumps(value, allow_nan=False, sort_keys=True)
