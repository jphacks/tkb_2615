"""Versioned long-term body and affect state for the registered life-state policy."""

from __future__ import annotations

import functools
import json
import math
import re
from typing import Any

from sheltersim import native
from sheltersim.core.history import records_since_previous_tick
from sheltersim.core.world import WorldState
from sheltersim.policies.affect import create_affect_policy

BODY_STATE_KEY = "body_state_v1"
BODY_POLICY_VERSION = "life-state-feedback-v1"
BODY_SOURCE_ID = "ASM-LIFE-STATE-FEEDBACK-V1"
MAX_SOCIAL_HISTORY = 20_000
MAX_SOCIAL_RELATIONSHIPS = 100_000

_DEFAULTS: dict[str, float] = {
    "awake_sleep_debt_minutes_per_minute": 1.0,
    "rest_sleep_debt_recovery_per_minute": 0.25,
    "sleep_sleep_debt_recovery_per_minute": 1.0,
    "sleep_debt_stress_rate": 0.001,
    "uncertainty_drift_per_minute": 0.00002,
    "uncertainty_decay_per_minute": 0.00005,
    "discontent_stress_rate": 0.001,
    "discontent_queue_rate": 0.0005,
    "discontent_relief_per_minute": 0.0002,
    "reassurance_decay_per_minute": 0.0003,
    "positive_affect_decay_per_minute": 0.0001,
    "negative_affect_stress_rate": 0.001,
    "hygiene_growth_per_minute": 0.00005,
    "sanitation_hygiene_growth_per_minute": 0.0001,
    "sanitation_stress_rate": 0.002,
    "affect_transfer_rate": 0.05,
    "relationship_trust_gain": 0.0005,
    "relationship_trust_decay_per_day": 0.002,
    "relationship_repeat_contact_bonus": 0.05,
    "cooperation_positive_gain": 0.03,
    "cooperation_negative_reduction": 0.02,
    "cooperation_reassurance_gain": 0.03,
    "cooperation_discontent_reduction": 0.01,
    "social_action_affect_transfer": 0.02,
    "complaint_reassurance_gain": 0.02,
    "complaint_discontent_reduction": 0.01,
    "hygiene_support_relief": 0.15,
    "caregiver_assistance_stress_relief": 0.03,
    "caregiver_assistance_reassurance_gain": 0.02,
    "privacy_accommodation_discontent_relief": 0.02,
    "mobility_assistance_stress_relief": 0.03,
    "language_access_uncertainty_relief": 0.05,
    "medical_assessment_uncertainty_relief": 0.02,
    "medication_support_uncertainty_relief": 0.01,
    "dependent_assistance_stress_relief": 0.04,
    "dependent_assistance_reassurance_gain": 0.03,
    "correction_uncertainty_reduction": 0.05,
    "correction_reassurance_gain": 0.01,
}


def canonical_body_config(raw: Any) -> dict[str, Any]:
    """Validate explicit units/ranges; arbitrary policy expressions are never accepted."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("body_dynamics config must be an object")
    allowed = {
        "schema_version",
        "policy_version",
        "source_id",
        *_DEFAULTS,
        "social_interaction_minutes",
        "affect_policy",
        "relationship_distance_scale_m",
        "sleep_debt_stress_rate_by_agent",
        "negative_affect_stress_rate_by_agent",
        "discontent_stress_rate_by_agent",
        "complaint_reassurance_gain_by_agent",
        "hygiene_support_relief_by_agent",
        "caregiver_assistance_stress_relief_by_agent",
        "caregiver_assistance_reassurance_gain_by_agent",
        "privacy_accommodation_discontent_relief_by_agent",
        "mobility_assistance_stress_relief_by_agent",
        "language_access_uncertainty_relief_by_agent",
        "medical_assessment_uncertainty_relief_by_agent",
        "medication_support_uncertainty_relief_by_agent",
        "dependent_assistance_stress_relief_by_agent",
        "dependent_assistance_reassurance_gain_by_agent",
    }
    if set(raw) - allowed:
        raise ValueError("body_dynamics config contains unsupported fields")
    config = {
        "schema_version": 1,
        "policy_version": BODY_POLICY_VERSION,
        "source_id": BODY_SOURCE_ID,
        **_DEFAULTS,
        "social_interaction_minutes": 5,
        "affect_policy": "linear-positive-negative-v1",
        "relationship_distance_scale_m": 2.0,
        **raw,
    }
    if (
        config["schema_version"] != 1
        or config["policy_version"] != BODY_POLICY_VERSION
        or config["source_id"] != BODY_SOURCE_ID
    ):
        raise ValueError("unsupported body_dynamics policy or source")
    for name in _DEFAULTS:
        value = config[name]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not 0.0 <= value <= 1.0
        ):
            raise ValueError(f"body_dynamics {name} must be finite and within 0..1")
    if (
        type(config["social_interaction_minutes"]) is not int
        or not 1 <= config["social_interaction_minutes"] <= 1440
    ):
        raise ValueError("body_dynamics social_interaction_minutes must be within 1..1440")
    if not isinstance(config["affect_policy"], str):
        raise ValueError("body_dynamics affect_policy must be an installed model ID")
    create_affect_policy(config["affect_policy"])
    distance_scale = config["relationship_distance_scale_m"]
    if (
        not isinstance(distance_scale, (int, float))
        or isinstance(distance_scale, bool)
        or not math.isfinite(distance_scale)
        or not 0.1 <= distance_scale <= 100.0
    ):
        raise ValueError(
            "body_dynamics relationship_distance_scale_m must be within 0.1..100 metres"
        )
    if config["awake_sleep_debt_minutes_per_minute"] > 10.0:
        raise ValueError("awake sleep debt rate cannot exceed 10 minutes per minute")
    if (
        config["rest_sleep_debt_recovery_per_minute"] > 10.0
        or config["sleep_sleep_debt_recovery_per_minute"] > 10.0
    ):
        raise ValueError("sleep debt recovery cannot exceed 10 minutes per minute")
    for field, label in (
        ("sleep_debt_stress_rate_by_agent", "sleep debt rates"),
        ("negative_affect_stress_rate_by_agent", "negative-affect stress rates"),
        ("discontent_stress_rate_by_agent", "discontent stress rates"),
        ("complaint_reassurance_gain_by_agent", "reassurance gains"),
        ("hygiene_support_relief_by_agent", "hygiene support relief"),
        ("caregiver_assistance_stress_relief_by_agent", "caregiver stress relief"),
        ("caregiver_assistance_reassurance_gain_by_agent", "caregiver reassurance gains"),
        ("privacy_accommodation_discontent_relief_by_agent", "privacy discontent relief"),
        ("mobility_assistance_stress_relief_by_agent", "mobility stress relief"),
        ("language_access_uncertainty_relief_by_agent", "language-access uncertainty relief"),
        ("medical_assessment_uncertainty_relief_by_agent", "medical-support uncertainty relief"),
        ("medication_support_uncertainty_relief_by_agent", "medication-support uncertainty relief"),
        ("dependent_assistance_stress_relief_by_agent", "dependent-assistance stress relief"),
        (
            "dependent_assistance_reassurance_gain_by_agent",
            "dependent-assistance reassurance gains",
        ),
    ):
        per_agent = config.get(field)
        if per_agent is None:
            continue
        if not isinstance(per_agent, dict) or not per_agent:
            raise ValueError(f"body_dynamics per-Agent {label} must be a non-empty ID map")
        for agent_id, rate in per_agent.items():
            if (
                not isinstance(agent_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", agent_id)
                or type(rate) not in (int, float)
                or not math.isfinite(rate)
                or not 0.0 <= rate <= 1.0
            ):
                raise ValueError(f"body_dynamics per-Agent {label} are invalid")
    return config


def create_body_state(world: WorldState) -> dict[str, Any]:
    """Initialize a detached record for each active Agent before checkpoint zero."""
    config, _ = _body_config_for_world(world)
    return {
        "schema_version": 1,
        "policy_version": BODY_POLICY_VERSION,
        "source_id": BODY_SOURCE_ID,
        "config": config,
        "agents": {
            agent_id: {
                "sleep_debt_minutes": min(1440.0, agent.needs.fatigue * 360.0),
                "uncertainty": min(1.0, 0.25 + agent.stress * 0.5),
                "reassurance": 0.0,
                "discontent": agent.stress,
                "positive_affect": 0.5,
                "negative_affect": agent.stress,
                "hygiene_need": 0.0,
            }
            for agent_id, agent in sorted(world.agents.items())
        },
        "relationships": {},
        "history": [],
    }


def _relationship_key(key: Any) -> tuple[tuple[Any, ...] | None, bool] | None:
    """None if key is not JSON, else (its two items if it is a list of two, whether
    key is their compact JSON)."""
    try:
        pair = json.loads(key)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(pair, list) or len(pair) != 2:
        return None, False
    return tuple(pair), json.dumps(pair, separators=(",", ":")) == key


# Every validation, which runs at every tick, parses the same relationship keys, and
# the parse depends on the key alone (#228). Whether the people exist is checked
# every time.
_cached_relationship_key = functools.lru_cache(maxsize=65_536)(_relationship_key)


def _parse_key(key: Any) -> tuple[tuple[Any, ...] | None, bool] | None:
    return _cached_relationship_key(key) if type(key) is str else _relationship_key(key)


# The parse of each str key, for the Rust kernel to look up first; the parse depends
# on the key alone.
_PARSED_KEYS: dict[str, Any] = {}
_RELATIONSHIP_PROBLEMS = {
    1: "body_state_v1 relationship key is invalid",
    2: "body_state_v1 relationship record is invalid",
    3: "body_state_v1 trust must be within 0..1",
    4: "body_state_v1 contact minute is invalid",
}


_RELATIONSHIP_FIELDS = frozenset(
    {
        "contact_count",
        "trust",
        "contact_minutes",
        "last_contact_minute",
        "last_interaction",
        "topic_ids",
    }
)


def validate_body_state(world: WorldState, *, validate_event_history: bool = True) -> None:
    """Reject partial state, unknown people, out-of-range values and malformed evidence."""
    state = world.model_state.get(BODY_STATE_KEY)
    if state is None:
        return
    if (
        not isinstance(state, dict)
        or set(state)
        != {
            "schema_version",
            "policy_version",
            "source_id",
            "config",
            "agents",
            "relationships",
            "history",
        }
        or state.get("schema_version") != 1
        or state.get("policy_version") != BODY_POLICY_VERSION
        or state.get("source_id") != BODY_SOURCE_ID
        or not isinstance(state.get("agents"), dict)
        or set(state["agents"]) != set(world.agents)
        or not isinstance(state.get("relationships"), dict)
        or len(state["relationships"]) > MAX_SOCIAL_RELATIONSHIPS
        or not isinstance(state.get("history"), list)
        or len(state["history"]) > MAX_SOCIAL_HISTORY
    ):
        raise ValueError("body_state_v1 has an invalid exact schema")
    config = canonical_body_config(state["config"])
    per_agent_fields = (
        "sleep_debt_stress_rate_by_agent",
        "negative_affect_stress_rate_by_agent",
        "discontent_stress_rate_by_agent",
        "complaint_reassurance_gain_by_agent",
        "hygiene_support_relief_by_agent",
        "caregiver_assistance_stress_relief_by_agent",
        "caregiver_assistance_reassurance_gain_by_agent",
        "privacy_accommodation_discontent_relief_by_agent",
        "mobility_assistance_stress_relief_by_agent",
        "language_access_uncertainty_relief_by_agent",
        "medical_assessment_uncertainty_relief_by_agent",
        "medication_support_uncertainty_relief_by_agent",
        "dependent_assistance_stress_relief_by_agent",
        "dependent_assistance_reassurance_gain_by_agent",
    )
    configured_agent_ids = set().union(*(set(config.get(field, {})) for field in per_agent_fields))
    if configured_agent_ids - set(world.agents):
        population = world.model_state.get("population_dynamics_v1", {})
        historical_ids = set(world.agents)
        if isinstance(population, dict):
            historical_ids.update(population.get("initial_agent_ids", ()))
            historical_ids.update(population.get("profile_traits", {}))
        if configured_agent_ids - historical_ids:
            raise ValueError("body_state_v1 per-Agent parameter references an unknown resident")
    selected_affect = _registry_affect_model(world)
    registry_selected_affect = selected_affect is not None
    if selected_affect is not None and config["affect_policy"] != selected_affect:
        raise ValueError("body_state_v1 affect model does not match the shared Policy Registry")
    optional_compatibility_fields = {
        "social_interaction_minutes",
        "relationship_distance_scale_m",
        "relationship_repeat_contact_bonus",
        "hygiene_support_relief",
        "caregiver_assistance_stress_relief",
        "caregiver_assistance_reassurance_gain",
        "privacy_accommodation_discontent_relief",
        "mobility_assistance_stress_relief",
        "language_access_uncertainty_relief",
        "medical_assessment_uncertainty_relief",
        "medication_support_uncertainty_relief",
        "dependent_assistance_stress_relief",
        "dependent_assistance_reassurance_gain",
    }
    if not registry_selected_affect:
        optional_compatibility_fields.add("affect_policy")
    # The saved config may be the canonical one without some optional fields that
    # older versions did not write. Checking the fields directly accepts the same
    # configs as comparing with every such reduced config (up to 2**14), which ran
    # at every tick (#228).
    saved = state["config"]
    if (
        not isinstance(saved, dict)
        or not set(saved) <= set(config)
        or not set(config) - set(saved) <= optional_compatibility_fields
        or any(saved[key] is not config[key] and saved[key] != config[key] for key in saved)
    ):
        raise ValueError("body_state_v1 config is not canonical")
    fields = {
        "sleep_debt_minutes",
        "uncertainty",
        "reassurance",
        "discontent",
        "positive_affect",
        "negative_affect",
        "hygiene_need",
    }
    for agent_id, record in state["agents"].items():
        if not isinstance(record, dict) or set(record) != fields:
            raise ValueError(f"body_state_v1 record for {agent_id!r} is invalid")
        for name, value in record.items():
            upper = 1440.0 if name == "sleep_debt_minutes" else 1.0
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not 0.0 <= value <= upper
            ):
                raise ValueError(f"body_state_v1 {name} for {agent_id!r} is invalid")
    agents = world.agents
    problem = native.body_relationships_problem(
        state["relationships"], agents, _PARSED_KEYS, _parse_key
    )
    if problem:
        # The Rust kernel checked every relationship by the rules below, in order.
        raise ValueError(_RELATIONSHIP_PROBLEMS[problem])
    # Without the kernel (problem is None) this loop checks them. Every tick checks
    # every relationship, so each record is checked inline, accepting exactly what
    # the field-by-field rules accept.
    for key, record in state["relationships"].items() if problem is None else ():
        parsed = _cached_relationship_key(key) if type(key) is str else _relationship_key(key)
        if parsed is None:
            raise ValueError("body_state_v1 relationship key is invalid")
        pair, compact = parsed
        if (
            pair is None
            or pair[0] not in agents
            or pair[1] not in agents
            or pair[0] == pair[1]
            or not compact
            or not isinstance(record, dict)
            or record.keys() != _RELATIONSHIP_FIELDS
            or type(contact_count := record["contact_count"]) is not int
            or contact_count < 0
            or type(contact_minutes := record["contact_minutes"]) is not int
            or contact_minutes < 0
            or not isinstance(topic_ids := record["topic_ids"], list)
            or len(topic_ids) > 32
            or (
                topic_ids and any(not isinstance(item, str) or len(item) > 64 for item in topic_ids)
            )
        ):
            raise ValueError("body_state_v1 relationship record is invalid")
        trust = record["trust"]
        minute = record["last_contact_minute"]
        if trust is not None and (
            not isinstance(trust, (int, float)) or isinstance(trust, bool) or not 0 <= trust <= 1
        ):
            raise ValueError("body_state_v1 trust must be within 0..1")
        if minute is not None and (type(minute) is not int or minute < 0):
            raise ValueError("body_state_v1 contact minute is invalid")
    population = world.model_state.get("population_dynamics_v1", {})
    historical_ids = set(world.agents)
    if isinstance(population, dict):
        historical_ids.update(population.get("initial_agent_ids", ()))
        historical_ids.update(population.get("profile_traits", {}))
        historical_ids.update(
            item.get("agent_id")
            for item in population.get("departures", ())
            if isinstance(item, dict) and isinstance(item.get("agent_id"), str)
        )
    history = state["history"]
    if not validate_event_history:
        history = records_since_previous_tick(history, world.minute_index)
    for item in history:
        expected = {"event_id", "minute", "kind", "actor_ids", "topic_id", "causation_id", "deltas"}
        if (
            not isinstance(item, dict)
            or set(item) != expected
            or not isinstance(item["event_id"], str)
            or type(item["minute"]) is not int
            or item["minute"] < 0
            or not isinstance(item["kind"], str)
            or not isinstance(item["actor_ids"], list)
            or any(agent_id not in historical_ids for agent_id in item["actor_ids"])
            or not isinstance(item["deltas"], dict)
        ):
            raise ValueError("body_state_v1 social history item is invalid")


def add_body_agent(world: WorldState, agent_id: str) -> None:
    """Initialize a newly admitted Agent without inventing prior reassurance."""
    state = world.model_state.get(BODY_STATE_KEY)
    agent = world.agents.get(agent_id)
    if state is not None and agent is not None:
        state["agents"][agent_id] = {
            "sleep_debt_minutes": min(1440.0, agent.needs.fatigue * 360.0),
            "uncertainty": min(1.0, 0.25 + agent.stress * 0.5),
            "reassurance": 0.0,
            "discontent": agent.stress,
            "positive_affect": 0.5,
            "negative_affect": agent.stress,
            "hygiene_need": 0.0,
        }


def remove_body_agent(world: WorldState, agent_id: str) -> dict[str, Any] | None:
    """Remove active body/social state; population history retains the stable identity."""
    state = world.model_state.get(BODY_STATE_KEY)
    if state is None:
        return None
    removed = state["agents"].pop(agent_id, None)
    for key in sorted(tuple(state["relationships"])):
        if agent_id in json.loads(key):
            state["relationships"].pop(key)
    return removed


def record_social_event(world: WorldState, event: Any, minute: int) -> None:
    """Apply bounded affect and trust changes only from typed, applied simulation events."""
    state = world.model_state.get(BODY_STATE_KEY)
    if state is None:
        return
    payload = event.payload
    transfers: list[tuple[str, str, str, dict[str, float]]] = []
    if event.event_type == "proximity_contact_update":
        first, second = payload.get("first_agent_id"), payload.get("second_agent_id")
        observed_minute = payload.get("observed_minute")
        distance = payload.get("distance_m")
        duration = payload.get("contact_minutes")
        if (
            first in world.agents
            and second in world.agents
            and first != second
            and isinstance(observed_minute, int)
            and not isinstance(observed_minute, bool)
        ):
            for recipient, sender in ((first, second), (second, first)):
                _record_relationship(
                    state,
                    recipient,
                    sender,
                    observed_minute,
                    "observed_proximity_contact",
                    None,
                    duration,
                    distance_m=distance,
                    event_id=event.event_id,
                    causation_id=event.causation_id,
                    model_configs=world.model_configs,
                )
    elif event.event_type == "information_contact_update":
        sender, recipient = payload.get("sender_agent_id"), payload.get("recipient_agent_id")
        if sender in world.agents and recipient in world.agents and sender != recipient:
            source = state["agents"][sender]
            target = state["agents"][recipient]
            before = dict(target)
            rate = state["config"]["affect_transfer_rate"]
            from sheltersim.policies.registry import create_affect

            policy = create_affect(world.model_configs)
            contribution = policy.contact(
                source,
                target,
                rate=rate,
                relationship_trust=_relationship_trust(state, recipient, sender),
            )
            _apply_affect_contribution(target, contribution)
            transfers.append(
                (recipient, sender, "information_contact", _body_deltas(before, target))
            )
            _record_relationship(
                state,
                recipient,
                sender,
                minute,
                "information_contact",
                payload.get("message_id"),
                payload.get("contact_minutes", 1),
                distance_m=_recorded_contact_distance(world, recipient, sender),
                event_id=event.event_id,
                causation_id=event.causation_id,
                model_configs=world.model_configs,
            )
    elif event.event_type == "social_interaction":
        interaction = payload.get("interaction_type")
        actor = payload.get("agent_id")
        participant = payload.get("participant_agent_id")
        if interaction == "cooperation" and actor in world.agents and participant in world.agents:
            config = state["config"]
            from sheltersim.policies.registry import create_affect

            policy = create_affect(world.model_configs)
            for agent_id in (actor, participant):
                record = state["agents"][agent_id]
                before = dict(record)
                other_id = participant if agent_id == actor else actor
                trust_scale = policy.social_action(
                    1.0,
                    rate=1.0,
                    relationship_trust=_relationship_trust(state, agent_id, other_id),
                ).positive_affect
                record["positive_affect"] = _unit(
                    record["positive_affect"] + config["cooperation_positive_gain"] * trust_scale
                )
                record["negative_affect"] = _unit(
                    record["negative_affect"]
                    - config["cooperation_negative_reduction"] * trust_scale
                )
                record["reassurance"] = _unit(
                    record["reassurance"] + config["cooperation_reassurance_gain"] * trust_scale
                )
                record["discontent"] = _unit(
                    record["discontent"] - config["cooperation_discontent_reduction"] * trust_scale
                )
                transfers.append((actor, agent_id, "cooperation", _body_deltas(before, record)))
            duration = payload.get("interaction_duration_minutes", 1)
            _record_relationship(
                state,
                actor,
                participant,
                minute,
                "cooperation",
                payload.get("topic_id"),
                duration,
                distance_m=_recorded_contact_distance(world, actor, participant),
                event_id=event.event_id,
                causation_id=event.causation_id,
                model_configs=world.model_configs,
            )
            _record_relationship(
                state,
                participant,
                actor,
                minute,
                "cooperation",
                payload.get("topic_id"),
                duration,
                distance_m=_recorded_contact_distance(world, participant, actor),
                event_id=event.event_id,
                causation_id=event.causation_id,
                model_configs=world.model_configs,
            )
        elif (
            interaction in {"reassurance_exchange", "family_care", "voluntary_support"}
            and actor in world.agents
            and participant in world.agents
        ):
            actor_record = state["agents"][actor]
            target_record = state["agents"][participant]
            before = dict(target_record)
            signal = _unit(actor_record["positive_affect"] - actor_record["negative_affect"])
            if actor_record["positive_affect"] < actor_record["negative_affect"]:
                signal = -_unit(actor_record["negative_affect"] - actor_record["positive_affect"])
            from sheltersim.policies.registry import create_affect

            policy = create_affect(world.model_configs)
            contribution = policy.social_action(
                signal,
                rate=state["config"]["social_action_affect_transfer"],
                relationship_trust=_relationship_trust(state, participant, actor),
            )
            _apply_affect_contribution(
                target_record,
                contribution,
                include_uncertainty=interaction == "reassurance_exchange",
            )
            transfers.append((actor, participant, interaction, _body_deltas(before, target_record)))
            _record_relationship(
                state,
                participant,
                actor,
                minute,
                interaction,
                payload.get("topic_id"),
                payload.get("interaction_duration_minutes", 1),
                distance_m=_recorded_contact_distance(world, participant, actor),
                event_id=event.event_id,
                causation_id=event.causation_id,
                model_configs=world.model_configs,
            )
        elif interaction == "complaint_response" and actor in world.agents:
            record = state["agents"][actor]
            config = state["config"]
            before = dict(record)
            record["reassurance"] = _unit(
                record["reassurance"] + config["complaint_reassurance_gain"]
            )
            record["discontent"] = _unit(
                record["discontent"] - config["complaint_discontent_reduction"]
            )
            record["negative_affect"] = _unit(record["negative_affect"] - 0.01)
            transfers.append((actor, actor, "complaint_response", _body_deltas(before, record)))
    elif event.event_type == "information_correction":
        zone_id = payload.get("zone_id")
        from sheltersim.contracts.policies import selected_models

        evidence_aware = (
            selected_models(world.model_configs)["information"] == "evidence-aware-rumor-v1"
        )
        information_state = world.model_state.get("information_spread_v1", {})
        message_id = payload.get("message_id")
        for actor in sorted(
            agent_id for agent_id, agent in world.agents.items() if agent.zone_id == zone_id
        ):
            if evidence_aware:
                raw = information_state.get("beliefs", {}).get(actor, {}).get(message_id)
                if (
                    raw is None
                    or raw.get("last_event_id") != event.event_id
                    or not raw.get("last_correction_accepted", False)
                ):
                    continue
            record = state["agents"][actor]
            before = dict(record)
            record["uncertainty"] = _unit(
                record["uncertainty"] - state["config"]["correction_uncertainty_reduction"]
            )
            record["reassurance"] = _unit(
                record["reassurance"] + state["config"]["correction_reassurance_gain"]
            )
            record["negative_affect"] = _unit(record["negative_affect"] - 0.01)
            transfers.append((actor, actor, "information_correction", _body_deltas(before, record)))
    for actor, other, kind, deltas in transfers:
        distance = _recorded_contact_distance(world, actor, other) if actor != other else None
        if distance is not None:
            deltas["contact_distance_m"] = distance
        duration = payload.get("interaction_duration_minutes", payload.get("contact_minutes"))
        if (
            isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and math.isfinite(duration)
            and duration > 0
        ):
            deltas["contact_duration_minutes"] = float(duration)
        history = state["history"]
        history.append(
            {
                "event_id": event.event_id,
                "minute": minute,
                "kind": kind,
                "actor_ids": sorted({actor, other}),
                "topic_id": (
                    payload.get("message_id")
                    if isinstance(payload.get("message_id"), str)
                    else payload.get("topic_id")
                    if isinstance(payload.get("topic_id"), str)
                    else None
                ),
                "causation_id": event.causation_id,
                "deltas": deltas,
            }
        )
        if len(history) > MAX_SOCIAL_HISTORY:
            del history[: len(history) - MAX_SOCIAL_HISTORY]


def _body_deltas(before: dict[str, Any], after: dict[str, Any]) -> dict[str, float]:
    """Record only measured changes to fields that the typed event actually modified."""
    return {
        field: after[field] - before[field]
        for field in sorted(set(before) & set(after))
        if isinstance(before[field], (int, float))
        and isinstance(after[field], (int, float))
        and after[field] != before[field]
    }


def _recorded_contact_distance(world: WorldState, first_id: str, second_id: str) -> float | None:
    """Return observed coordinate distance only when both runtime positions exist."""
    movement = world.model_state.get("movement_v1")
    positions = movement.get("positions", {}) if isinstance(movement, dict) else {}
    first_record = positions.get(first_id, {}) if isinstance(positions, dict) else {}
    second_record = positions.get(second_id, {}) if isinstance(positions, dict) else {}
    first, second = first_record.get("point"), second_record.get("point")
    if (
        not isinstance(first, dict)
        or not isinstance(second, dict)
        or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in (*first.values(), *second.values())
        )
        or set(first) != {"x", "y"}
        or set(second) != {"x", "y"}
    ):
        return None
    return math.hypot(
        float(second["x"]) - float(first["x"]), float(second["y"]) - float(first["y"])
    )


def _relationship_trust(state: dict[str, Any], recipient: str, sender: str) -> float:
    key = json.dumps([recipient, sender], separators=(",", ":"))
    record = state["relationships"].get(key)
    value = record.get("trust") if isinstance(record, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.5


def _apply_affect_contribution(
    record: dict[str, Any], contribution: Any, *, include_uncertainty: bool = True
) -> None:
    for field in ("positive_affect", "negative_affect", "reassurance", "discontent"):
        record[field] = _unit(record[field] + getattr(contribution, field))
    if include_uncertainty:
        record["uncertainty"] = _unit(record["uncertainty"] + contribution.uncertainty)


def _record_relationship(
    state: dict[str, Any],
    recipient: str,
    sender: str,
    minute: int,
    kind: str,
    topic: Any,
    duration_minutes: Any = 1,
    *,
    distance_m: float | None = None,
    event_id: str | None = None,
    causation_id: str | None = None,
    model_configs: dict[str, dict[str, Any]] | None = None,
) -> None:
    key = json.dumps([recipient, sender], separators=(",", ":"))
    relationships = state["relationships"]
    record = relationships.get(key)
    if record is None:
        if len(relationships) >= MAX_SOCIAL_RELATIONSHIPS:
            return
        record = relationships[key] = {
            "contact_count": 0,
            "trust": 0.5,
            "contact_minutes": 0,
            "last_contact_minute": None,
            "last_interaction": None,
            "topic_ids": [],
        }
    previous_minute = record["last_contact_minute"]
    elapsed = 0 if previous_minute is None else max(0, minute - previous_minute)
    gain = state["config"]["relationship_trust_gain"]
    scale = max(1, state["config"].get("social_interaction_minutes", 5))
    duration = (
        float(duration_minutes)
        if isinstance(duration_minutes, (int, float))
        and not isinstance(duration_minutes, bool)
        and math.isfinite(duration_minutes)
        and duration_minutes >= 0
        else 1.0
    )
    from sheltersim.policies.relationships import create_relationship_policy

    policy = create_relationship_policy(model_configs or {})
    before_trust = float(record["trust"])
    record["trust"] = policy.update_trust(
        before_trust,
        duration_minutes=duration,
        interaction_scale_minutes=scale,
        prior_contact_count=record["contact_count"],
        elapsed_minutes=elapsed,
        trust_gain=gain,
        trust_decay_per_day=state["config"]["relationship_trust_decay_per_day"],
        distance_m=distance_m,
    )
    record["contact_count"] += 1
    record["contact_minutes"] += int(math.ceil(duration))
    record["last_contact_minute"] = minute
    record["last_interaction"] = kind
    if isinstance(topic, str) and topic not in record["topic_ids"]:
        record["topic_ids"] = (record["topic_ids"] + [topic])[-32:]
    history = state["history"]
    if event_id is not None:
        deltas: dict[str, Any] = {
            "relationship_trust_before": before_trust,
            "relationship_trust_after": record["trust"],
            "relationship_trust_delta": record["trust"] - before_trust,
            "contact_duration_minutes": duration,
            "relationship_policy_id": policy.model_id,
            "relationship_policy_version": policy.implementation_version,
            "relationship_policy_source_id": policy.source_id,
        }
        if distance_m is not None and math.isfinite(distance_m) and distance_m >= 0:
            deltas["contact_distance_m"] = distance_m
        history.append(
            {
                "event_id": event_id,
                "minute": minute,
                "kind": "relationship_update",
                "actor_ids": sorted({recipient, sender}),
                "topic_id": topic if isinstance(topic, str) else None,
                "causation_id": causation_id,
                "deltas": deltas,
            }
        )
        if len(history) > MAX_SOCIAL_HISTORY:
            del history[: len(history) - MAX_SOCIAL_HISTORY]


def _unit(value: float) -> float:
    return min(1.0, max(0.0, value))


def _registry_affect_model(world: WorldState) -> str | None:
    from sheltersim.contracts.policies import selected_models

    return selected_models(world.model_configs).get("affect")


def _body_config_for_world(world: WorldState) -> tuple[dict[str, Any], bool]:
    config = canonical_body_config(world.model_configs.get("body_dynamics"))
    selected = _registry_affect_model(world)
    if selected is not None:
        config["affect_policy"] = selected
    return config, selected is not None
