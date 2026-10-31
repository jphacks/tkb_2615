"""Directed, checkpoint-safe contact and relationship records for Agents."""

from __future__ import annotations

import functools
import json
import math
from typing import Any, Mapping

from sheltersim import native
from sheltersim.core.world import WorldState
from sheltersim.domain.models import ordered_pair_key
from sheltersim.events.models import ScheduledEvent

SOCIAL_NETWORK_STATE_KEY = "social_network_v1"
SOCIAL_NETWORK_POLICY_VERSION = "directed-contact-relationships-v1"
MAX_RELATIONSHIPS = 100_000
PROXIMITY_CONTACT_POLICY_VERSION = "distance-sampled-proximity-v1"
PROXIMITY_CONTACT_SOURCE_ID = "ASM-PROXIMITY-CONTACT-V1"
PROXIMITY_DISABLED_POLICY_VERSION = "disabled-proximity-v1"


def canonical_proximity_contact_config(raw: Any) -> dict[str, Any] | None:
    """Validate the opt-in physical-contact sampler's bounded assumptions."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != {
        "schema_version",
        "enabled",
        "policy_version",
        "sample_interval_minutes",
        "max_distance_m",
        "max_pairs_per_sample",
    }:
        raise ValueError("social_proximity config fields do not match")
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
        or type(raw["enabled"]) is not bool
        or raw["policy_version"] != PROXIMITY_CONTACT_POLICY_VERSION
    ):
        raise ValueError("social_proximity config version is unsupported")
    interval = raw["sample_interval_minutes"]
    maximum = raw["max_distance_m"]
    pair_limit = raw["max_pairs_per_sample"]
    if (
        type(interval) is not int
        or not 1 <= interval <= 60
        or isinstance(maximum, bool)
        or not isinstance(maximum, (int, float))
        or not math.isfinite(float(maximum))
        or not 0.25 <= float(maximum) <= 10.0
        or type(pair_limit) is not int
        or not 1 <= pair_limit <= 10_000
    ):
        raise ValueError("social_proximity bounds are invalid")
    if not raw["enabled"]:
        return None
    return {
        "schema_version": 1,
        "enabled": True,
        "policy_version": PROXIMITY_CONTACT_POLICY_VERSION,
        "sample_interval_minutes": interval,
        "max_distance_m": float(maximum),
        "max_pairs_per_sample": pair_limit,
    }


def create_social_network_state() -> dict[str, Any]:
    """Return an empty versioned sparse social graph."""
    return {
        "schema_version": 1,
        "policy_version": SOCIAL_NETWORK_POLICY_VERSION,
        "relationships": {},
        "interaction_count": 0,
        "information_transfer_count": 0,
    }


def _pair_key_problem(pair_key: Any) -> str | None:
    """None for the compact JSON of two different strings, else what is wrong."""
    try:
        pair = json.loads(pair_key)
    except (TypeError, json.JSONDecodeError):
        return "unparsable"
    if (
        not isinstance(pair, list)
        or len(pair) != 2
        or any(not isinstance(item, str) for item in pair)
        or pair[0] == pair[1]
        or json.dumps(pair, separators=(",", ":")) != pair_key
    ):
        return "not a pair"
    return None


# Every validation, which runs at every tick, checks the same relationship keys, and a
# key's check depends on the key alone (#228).
_cached_pair_key_problem = functools.lru_cache(maxsize=65_536)(_pair_key_problem)


def _key_problem(pair_key: Any) -> str | None:
    return (
        _cached_pair_key_problem(pair_key) if type(pair_key) is str else _pair_key_problem(pair_key)
    )


def _key_is_good(pair_key: Any) -> bool:
    return _key_problem(pair_key) is None


# str keys whose check found no problem, for the Rust kernel to look up first.
_GOOD_PAIR_KEYS: set[str] = set()


_RELATIONSHIP_FIELDS = frozenset(
    {
        "contact_count",
        "conversation_count",
        "cooperation_count",
        "trust",
        "shared_message_ids",
        "last_contact_minute",
        "last_interaction",
    }
)


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_social_network_state(world: WorldState, *, tick_memo: Any = None) -> None:
    """Validate sparse directed edges without requiring every possible Agent pair."""
    state = world.model_state.get(SOCIAL_NETWORK_STATE_KEY)
    if state is None:
        return
    if (
        not isinstance(state, dict)
        or set(state)
        != {
            "schema_version",
            "policy_version",
            "relationships",
            "interaction_count",
            "information_transfer_count",
        }
        or state.get("schema_version") != 1
        or state.get("policy_version") != SOCIAL_NETWORK_POLICY_VERSION
        or not isinstance(state.get("relationships"), dict)
        or len(state["relationships"]) > MAX_RELATIONSHIPS
    ):
        raise ValueError("social_network_v1 has an invalid exact schema")
    relationships = state["relationships"]
    if tick_memo is not None:
        relationships = tick_memo.relationships_to_check(tick_memo.social_watch, relationships)
    if native.social_relationships_valid(relationships, _GOOD_PAIR_KEYS, _key_is_good):
        return
    # Without the kernel, or for a value it leaves to Python, these rules decide. Every
    # tick checks every relationship, so each record is checked inline, accepting
    # exactly what the field-by-field rules accept.
    for pair_key, record in state["relationships"].items():
        problem = (
            _cached_pair_key_problem(pair_key)
            if type(pair_key) is str
            else _pair_key_problem(pair_key)
        )
        if problem == "unparsable":
            raise ValueError("social_network_v1 relationship key is invalid")
        if (
            problem is not None
            or not isinstance(record, dict)
            or record.keys() != _RELATIONSHIP_FIELDS
            or not _is_count(record["contact_count"])
            or not _is_count(record["conversation_count"])
            or not _is_count(record["cooperation_count"])
            or not isinstance(record["shared_message_ids"], list)
        ):
            raise ValueError("social_network_v1 relationship record is invalid")
        trust = record["trust"]
        if trust is not None and (
            not isinstance(trust, (int, float))
            or isinstance(trust, bool)
            or not 0.0 <= trust <= 1.0
        ):
            raise ValueError("social_network_v1 trust value is invalid")
        minute = record["last_contact_minute"]
        if minute is not None and (
            not isinstance(minute, int) or isinstance(minute, bool) or minute < 0
        ):
            raise ValueError("social_network_v1 contact time is invalid")


def record_contact(
    world: WorldState,
    *,
    recipient_id: str,
    sender_id: str,
    minute: int,
    message_id: str | None = None,
) -> None:
    """Record a local directed conversation and its information exchange."""
    if recipient_id == sender_id:
        return
    state = _state(world)
    record = _relationship(state, recipient_id, sender_id)
    record["contact_count"] += 1
    record["conversation_count"] += 1
    record["last_contact_minute"] = minute
    if message_id is not None and message_id not in record["shared_message_ids"]:
        if len(record["shared_message_ids"]) >= 64:
            record["shared_message_ids"].pop(0)
        record["shared_message_ids"].append(message_id)
        state["information_transfer_count"] += 1
    state["interaction_count"] += 1


def record_observed_contact(
    world: WorldState, *, recipient_id: str, source_id: str, minute: int
) -> None:
    """Record a sampled nearby contact without claiming that dialogue occurred."""
    if recipient_id == source_id:
        return
    state = _state(world)
    record = _relationship(state, recipient_id, source_id)
    record["contact_count"] += 1
    record["last_contact_minute"] = minute
    state["interaction_count"] += 1


def record_observed_contacts(world: WorldState, pairs: list[tuple[str, str]], minute: int) -> None:
    """record_observed_contact of each (recipient, source), for the Rust social kernel."""
    for recipient_id, source_id in pairs:
        record_observed_contact(
            world, recipient_id=recipient_id, source_id=source_id, minute=minute
        )


def record_interaction(
    world: WorldState,
    *,
    actor_id: str,
    participant_id: str,
    minute: int,
    interaction: str,
    trust_delta: float | None = None,
    directed_trust_deltas: tuple[float | None, float | None] | None = None,
) -> None:
    """Record a typed social action and its existing policy trust adjustment."""
    if actor_id == participant_id:
        return
    state = _state(world)
    for index, (recipient, source) in enumerate(
        ((actor_id, participant_id), (participant_id, actor_id))
    ):
        record = _relationship(state, recipient, source)
        record["conversation_count"] += 1
        record["last_contact_minute"] = minute
        record["last_interaction"] = interaction
        if interaction == "cooperation":
            record["cooperation_count"] += 1
        directional_delta = (
            directed_trust_deltas[index] if directed_trust_deltas is not None else trust_delta
        )
        if directional_delta is not None:
            current = record["trust"]
            record["trust"] = min(
                1.0, max(0.0, (0.5 if current is None else current) + directional_delta)
            )
    state["interaction_count"] += 1


def observe_event(world: WorldState, event: ScheduledEvent, minute: int) -> None:
    """Extract only allowlisted, structured contact data from an applied event."""
    payload = event.payload
    if event.event_type == "information_contact_update":
        recipient_id = payload.get("recipient_agent_id")
        sender_id = payload.get("sender_agent_id")
        message_id = payload.get("message_id")
        if isinstance(recipient_id, str) and isinstance(sender_id, str):
            record_contact(
                world,
                recipient_id=recipient_id,
                sender_id=sender_id,
                minute=minute,
                message_id=message_id if isinstance(message_id, str) else None,
            )
            _sync_body_trust(world, recipient_id, sender_id)
        return
    if event.event_type == "proximity_contact_update":
        first_id = payload.get("first_agent_id")
        second_id = payload.get("second_agent_id")
        observed_minute = payload.get("observed_minute")
        if (
            isinstance(first_id, str)
            and isinstance(second_id, str)
            and isinstance(observed_minute, int)
            and not isinstance(observed_minute, bool)
        ):
            for recipient_id, source_id in ((first_id, second_id), (second_id, first_id)):
                record_observed_contact(
                    world,
                    recipient_id=recipient_id,
                    source_id=source_id,
                    minute=observed_minute,
                )
                _sync_body_trust(world, recipient_id, source_id)
        return
    if event.event_type != "social_interaction":
        return
    interaction = payload.get("interaction_type")
    actor_id = payload.get("agent_id")
    participant_id = payload.get("participant_agent_id")
    if (
        interaction
        in {
            "cooperation",
            "information_reaction",
            "reassurance_exchange",
            "family_care",
            "voluntary_support",
        }
        and isinstance(actor_id, str)
        and isinstance(participant_id, str)
    ):
        # Cooperation applies the existing +0.01 per-Agent trust update in its
        # handler; the graph mirrors that established effect without a second model.
        directed_deltas = None
        delta = 0.01 if interaction == "cooperation" else None
        if interaction in {"reassurance_exchange", "family_care", "voluntary_support"}:
            body_state = world.model_state.get("body_state_v1", {})
            body_records = body_state.get("agents", {}) if isinstance(body_state, dict) else {}
            actor_record = body_records.get(actor_id, {})
            config = body_state.get("config", {}) if isinstance(body_state, dict) else {}
            signal = min(
                1.0,
                max(
                    -1.0,
                    actor_record.get("positive_affect", 0.5)
                    - actor_record.get("negative_affect", 0.0),
                ),
            )
            relief = config.get("social_action_affect_transfer", 0.02) * signal
            actor_delta = (
                max(0.0, relief * 0.25)
                if interaction in {"family_care", "voluntary_support"}
                else 0.0
            )
            directed_deltas = (actor_delta, relief * 0.5)
        record_interaction(
            world,
            actor_id=actor_id,
            participant_id=participant_id,
            minute=minute,
            interaction=interaction,
            trust_delta=delta,
            directed_trust_deltas=directed_deltas,
        )
        _sync_body_trust(world, actor_id, participant_id)
        _sync_body_trust(world, participant_id, actor_id)


def add_agent(world: WorldState, agent_id: str) -> None:
    """New Agents receive no invented prior relationships."""
    del world, agent_id


def remove_agent(world: WorldState, agent_id: str) -> list[dict[str, Any]]:
    """Detach a departed Agent's ties for immutable population departure history."""
    state = world.model_state.get(SOCIAL_NETWORK_STATE_KEY)
    if state is None:
        return []
    archived = []
    for pair_key in sorted(tuple(state["relationships"])):
        pair = json.loads(pair_key)
        if agent_id in pair:
            archived.append({"pair": pair, "record": state["relationships"].pop(pair_key)})
    return archived


def _state(world: WorldState) -> dict[str, Any]:
    if SOCIAL_NETWORK_STATE_KEY not in world.model_state:
        world.model_state[SOCIAL_NETWORK_STATE_KEY] = create_social_network_state()
    return world.model_state[SOCIAL_NETWORK_STATE_KEY]


def _relationship(state: dict[str, Any], recipient_id: str, sender_id: str) -> dict[str, Any]:
    key = ordered_pair_key(recipient_id, sender_id)
    relationships = state["relationships"]
    if key not in relationships:
        if len(relationships) >= MAX_RELATIONSHIPS:
            raise ValueError("social_network_v1 reached the configured relationship bound")
        relationships[key] = {
            "contact_count": 0,
            "conversation_count": 0,
            "cooperation_count": 0,
            "trust": None,
            "shared_message_ids": [],
            "last_contact_minute": None,
            "last_interaction": None,
        }
    return relationships[key]


def _sync_body_trust(world: WorldState, recipient_id: str, source_id: str) -> None:
    """Mirror the body policy's one computed directed trust value into the graph."""
    body = world.model_state.get("body_state_v1", {})
    body_relationships = body.get("relationships", {}) if isinstance(body, dict) else {}
    key = ordered_pair_key(recipient_id, source_id)
    body_record = body_relationships.get(key) if isinstance(body_relationships, dict) else None
    social = world.model_state.get(SOCIAL_NETWORK_STATE_KEY)
    social_record = (
        social.get("relationships", {}).get(key)
        if isinstance(social, dict) and isinstance(social.get("relationships"), dict)
        else None
    )
    if body_record is not None and social_record is not None:
        social_record["trust"] = body_record["trust"]
