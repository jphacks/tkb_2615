"""Versioned persistence helpers for optional information diffusion state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from sheltersim.domain.models import (
    AgentInformationState,
    InformationBeliefStatus,
    InformationTruthStatus,
    TypedMessage,
    validate_unit_interval,
)

INFORMATION_SPREAD_STATE_KEY = "information_spread_v1"
_STATE_SCHEMA_VERSION = 2
_COUNTER_FIELDS = {
    "contact_attempts",
    "contact_deliveries",
    "correction_deliveries",
    "support_requests",
}


def create_information_runtime_state(agent_ids: set[str]) -> dict[str, Any]:
    """Return the canonical empty model state for a configured run."""
    ordered_agents = sorted(agent_ids)
    return {
        "schema_version": _STATE_SCHEMA_VERSION,
        "messages": {},
        "beliefs": {agent_id: {} for agent_id in ordered_agents},
        "contacts": {},
        "query_pressure": {agent_id: 0.0 for agent_id in ordered_agents},
        "support_requests": [],
        "counters": {
            "contact_attempts": 0,
            "contact_deliveries": 0,
            "correction_deliveries": 0,
            "support_requests": 0,
        },
    }


def upgrade_information_runtime_state(state: dict[str, Any]) -> dict[str, Any]:
    """Migrate v1 persisted beliefs to the v2 cumulative-summary fields in place."""
    version = state.get("schema_version")
    if version == _STATE_SCHEMA_VERSION:
        return state
    if version != 1:
        raise ValueError("unsupported information-spread runtime state schema")
    state["schema_version"] = _STATE_SCHEMA_VERSION
    for agent_beliefs in state.get("beliefs", {}).values():
        for message_id, raw_belief in list(agent_beliefs.items()):
            belief = AgentInformationState.from_dict(raw_belief)
            message_raw = state.get("messages", {}).get(message_id)
            message = TypedMessage.from_dict(message_raw) if message_raw is not None else None
            ever_misbelieved = belief.ever_misbelieved or (
                message is not None
                and (
                    message.truth_status is InformationTruthStatus.TRUE
                    and belief.belief_strength < 0.5
                    or message.truth_status is InformationTruthStatus.FALSE
                    and belief.belief_strength > 0.5
                )
            )
            agent_beliefs[message_id] = replace(
                belief,
                ever_misbelieved=ever_misbelieved,
                ever_forgotten=(
                    belief.ever_forgotten or belief.belief is InformationBeliefStatus.UNKNOWN
                ),
            ).to_dict()
    return state


def validate_information_runtime_state(
    state: Any,
    *,
    agent_ids: set[str],
    zone_ids: set[str],
    duration_minutes: int,
) -> None:
    """Reject malformed or dangling persisted information state."""
    expected = {
        "schema_version",
        "messages",
        "beliefs",
        "contacts",
        "query_pressure",
        "support_requests",
        "counters",
    }
    if not isinstance(state, dict) or set(state) != expected:
        raise ValueError("information-spread runtime state fields do not match the schema")
    if (
        not isinstance(state["schema_version"], int)
        or isinstance(state["schema_version"], bool)
        or state["schema_version"] not in {1, _STATE_SCHEMA_VERSION}
    ):
        raise ValueError("unsupported information-spread runtime state schema")

    messages = state["messages"]
    if not isinstance(messages, dict) or len(messages) > 1024:
        raise ValueError("information-spread messages must be a bounded object")
    for message_id, raw_message in messages.items():
        message = TypedMessage.from_dict(raw_message)
        if message_id != message.id:
            raise ValueError("information message key must match its ID")
        if message.zone_id not in zone_ids or message.origin_agent_id not in agent_ids:
            raise ValueError("information message references an unknown zone or Agent")
        if message.time >= duration_minutes:
            raise ValueError("information message time is outside the scenario")

    beliefs = state["beliefs"]
    if not isinstance(beliefs, dict) or set(beliefs) != agent_ids:
        raise ValueError("information beliefs must contain exactly the known Agents")
    for agent_id, agent_beliefs in beliefs.items():
        if not isinstance(agent_beliefs, dict):
            raise ValueError("Agent information beliefs must be an object")
        for message_id, raw_belief in agent_beliefs.items():
            if state["schema_version"] == _STATE_SCHEMA_VERSION and (
                not isinstance(raw_belief, dict)
                or set(raw_belief)
                != {
                    "message_id",
                    "belief",
                    "source",
                    "last_contact",
                    "last_event_id",
                    "exposed",
                    "belief_strength",
                    "share_propensity",
                    "correction_exposed",
                    "correction_accepted",
                    "ever_misbelieved",
                    "ever_forgotten",
                    "last_correction_accepted",
                }
            ):
                raise ValueError("v2 Agent information belief fields do not match schema")
            belief = AgentInformationState.from_dict(raw_belief)
            if message_id not in messages or belief.message_id != message_id:
                raise ValueError("Agent information belief references an unknown message")
            if belief.last_contact >= duration_minutes:
                raise ValueError("information contact time is outside the scenario")
            if not isinstance(agent_id, str):
                raise ValueError("information belief Agent IDs must be strings")

    contacts = state["contacts"]
    if not isinstance(contacts, dict) or len(contacts) > len(agent_ids) * 16:
        raise ValueError("information contacts must be a bounded object")
    for pair_key, record in contacts.items():
        try:
            pair = json.loads(pair_key)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("information contact key is invalid") from exc
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(not isinstance(agent_id, str) for agent_id in pair)
            or pair[0] not in agent_ids
            or pair[1] not in agent_ids
            or pair[0] == pair[1]
            or json.dumps(pair, separators=(",", ":")) != pair_key
        ):
            raise ValueError("information contact key must identify an Agent pair")
        if not isinstance(record, dict) or set(record) != {
            "last_seen_minute",
            "consecutive_minutes",
        }:
            raise ValueError("information contact record fields do not match")
        for name in ("last_seen_minute", "consecutive_minutes"):
            value = record[name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"information contact {name} must be non-negative")
        if (
            record["last_seen_minute"] >= duration_minutes
            or record["consecutive_minutes"] < 1
            or record["consecutive_minutes"] > record["last_seen_minute"] + 1
        ):
            raise ValueError("information contact duration is invalid")

    query_pressure = state["query_pressure"]
    if not isinstance(query_pressure, dict) or set(query_pressure) != agent_ids:
        raise ValueError("information query pressure must contain exactly the known Agents")
    for agent_id, pressure in query_pressure.items():
        validate_unit_interval(f"information query pressure for {agent_id}", pressure)

    support_requests = state["support_requests"]
    if not isinstance(support_requests, list):
        raise ValueError("information support requests must be a list")
    request_ids: set[str] = set()
    for request in support_requests:
        if not isinstance(request, dict) or set(request) != {
            "request_id",
            "agent_id",
            "minute",
            "message_ids",
        }:
            raise ValueError("information support request fields do not match")
        request_id = request["request_id"]
        agent_id = request["agent_id"]
        minute = request["minute"]
        message_ids = request["message_ids"]
        if (
            not isinstance(request_id, str)
            or not request_id
            or request_id in request_ids
            or not isinstance(agent_id, str)
            or agent_id not in agent_ids
            or not isinstance(minute, int)
            or isinstance(minute, bool)
            or not 0 <= minute < duration_minutes
            or not isinstance(message_ids, list)
            or not message_ids
            or any(not isinstance(message_id, str) for message_id in message_ids)
            or len(message_ids) != len(set(message_ids))
            or any(message_id not in messages for message_id in message_ids)
        ):
            raise ValueError("information support request reference is invalid")
        request_ids.add(request_id)

    counters = state["counters"]
    if not isinstance(counters, dict) or set(counters) != _COUNTER_FIELDS:
        raise ValueError("information-spread counters do not match the schema")
    for name, value in counters.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"information-spread counter {name} must be non-negative")
    if counters["support_requests"] != len(support_requests):
        raise ValueError("information support-request counter does not match its records")


def information_spread_summary(state: Mapping[str, Any], *, agent_count: int) -> dict[str, Any]:
    """Compute explainable reach/correction rates from persisted Agent-message beliefs."""
    messages = state["messages"]
    beliefs = state["beliefs"]
    exposed_states = [
        belief for agent_beliefs in beliefs.values() for belief in agent_beliefs.values()
    ]
    reached = sum(
        1
        for belief in exposed_states
        if AgentInformationState.from_dict(belief).belief is not InformationBeliefStatus.UNKNOWN
    )
    exposed = len(exposed_states)
    correction_exposures = sum(
        1
        for raw_belief in exposed_states
        if AgentInformationState.from_dict(raw_belief).correction_exposed
    )
    correction_acceptances = sum(
        1
        for raw_belief in exposed_states
        if AgentInformationState.from_dict(raw_belief).correction_accepted
    )
    wrong_agent_message_pairs: set[tuple[str, str]] = set()
    ever_wrong_agents: set[str] = set()
    forgotten_agents: set[str] = set()
    currently_forgotten_agents: set[str] = set()
    correction_exposed_agents: set[str] = set()
    correction_accepted_agents: set[str] = set()
    currently_wrong_agents: set[str] = set()
    for agent_id, agent_beliefs in beliefs.items():
        for raw_belief in agent_beliefs.values():
            belief = AgentInformationState.from_dict(raw_belief)
            message = TypedMessage.from_dict(messages[belief.message_id])
            if belief.ever_misbelieved:
                ever_wrong_agents.add(agent_id)
            if belief.ever_forgotten:
                forgotten_agents.add(agent_id)
                if belief.belief is InformationBeliefStatus.UNKNOWN:
                    currently_forgotten_agents.add(agent_id)
            if belief.correction_exposed:
                correction_exposed_agents.add(agent_id)
            if belief.correction_accepted:
                correction_accepted_agents.add(agent_id)
            is_wrong = (
                message.truth_status is InformationTruthStatus.TRUE and belief.belief_strength < 0.5
            ) or (
                message.truth_status is InformationTruthStatus.FALSE
                and belief.belief_strength > 0.5
            )
            if is_wrong:
                ever_wrong_agents.add(agent_id)
                if belief.belief is not InformationBeliefStatus.UNKNOWN:
                    wrong_agent_message_pairs.add((agent_id, belief.message_id))
                    currently_wrong_agents.add(agent_id)
    possible_pairs = agent_count * len(messages)
    reach_rate = reached / possible_pairs if possible_pairs else 0.0
    correction_rate = correction_acceptances / reached if reached else 0.0
    return {
        "message_count": len(messages),
        "exposure_count": exposed,
        "agent_message_reach_count": reached,
        "possible_agent_message_pairs": possible_pairs,
        "reach_rate": reach_rate,
        "correction_count": correction_acceptances,
        "correction_exposures": correction_exposures,
        "correction_acceptances": correction_acceptances,
        "wrong_belief_count": len(wrong_agent_message_pairs),
        "current_wrong_belief_count": len(currently_wrong_agents),
        "current_wrong_agent_message_count": len(wrong_agent_message_pairs),
        "ever_wrong_belief_count": len(ever_wrong_agents),
        "currently_forgotten_count": len(currently_forgotten_agents),
        "ever_forgotten_count": len(forgotten_agents),
        "correction_exposed_agent_count": len(correction_exposed_agents),
        "correction_accepted_agent_count": len(correction_accepted_agents),
        "correction_rate": correction_rate,
        "correction_acceptance_rate": (
            correction_acceptances / correction_exposures if correction_exposures else 0.0
        ),
        "support_requests": state["counters"]["support_requests"],
        "counters": dict(state["counters"]),
    }
