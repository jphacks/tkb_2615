"""Checkpoint-safe autonomy memory and facility awareness for synthetic Agents."""

from __future__ import annotations

from typing import Any

from sheltersim.core.world import WorldState
from sheltersim.domain.facility_awareness import FacilityAwarenessConfig
from sheltersim.policies.behavior import ActionProposal

AGENT_DYNAMICS_STATE_KEY = "agent_dynamics_v1"
AGENT_DYNAMICS_POLICY_VERSION = "need-led-autonomy-memory-v1"
ORIENTATION_AGENT_DYNAMICS_POLICY_VERSION = "need-led-autonomy-memory-v2"
# v3 keeps the v2 intake orientation and also announces shared facilities that
# are installed during a Run to the Agents already present.
ANNOUNCED_FACILITY_AGENT_DYNAMICS_POLICY_VERSION = "need-led-autonomy-memory-v3"
ORIENTATION_FACILITY_SOURCE_ID = "ASM-INTAKE-FACILITY-ORIENTATION-V1"
INSTALLED_FACILITY_ANNOUNCEMENT_SOURCE_ID = "ASM-INSTALLED-FACILITY-ANNOUNCEMENT-V1"
_ORIENTED_POLICY_VERSIONS = frozenset(
    {
        ORIENTATION_AGENT_DYNAMICS_POLICY_VERSION,
        ANNOUNCED_FACILITY_AGENT_DYNAMICS_POLICY_VERSION,
    }
)
# Shared services every evacuee is assumed to be shown at intake. Group-specific
# spaces (pet, family, privacy) are still learned only on arrival.
ORIENTATION_FACILITY_KINDS = frozenset(
    {
        "intake",
        "meal_distribution",
        "medical_support",
        "quiet_space",
        "toilet",
        "water_station",
    }
)


def canonical_agent_dynamics_config(raw: Any) -> dict[str, Any]:
    """Select the facility-awareness version; a missing config keeps v1 histories exact."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - {"schema_version", "policy_version", "source_id"}:
        raise ValueError("agent_dynamics config has unsupported fields")
    policy_version = raw.get("policy_version", AGENT_DYNAMICS_POLICY_VERSION)
    if raw.get("schema_version", 1) != 1 or policy_version not in {
        AGENT_DYNAMICS_POLICY_VERSION,
        *_ORIENTED_POLICY_VERSIONS,
    }:
        raise ValueError("agent_dynamics config has an unsupported version")
    if policy_version == AGENT_DYNAMICS_POLICY_VERSION:
        if "source_id" in raw:
            raise ValueError("agent_dynamics v1 has no orientation source")
        return {"schema_version": 1, "policy_version": policy_version}
    if raw.get("source_id", ORIENTATION_FACILITY_SOURCE_ID) != ORIENTATION_FACILITY_SOURCE_ID:
        raise ValueError("agent_dynamics v2/v3 must cite the intake orientation assumption")
    return {
        "schema_version": 1,
        "policy_version": policy_version,
        "source_id": ORIENTATION_FACILITY_SOURCE_ID,
    }


def _initial_known_facility_ids(world: WorldState, zone_id: str) -> list[str]:
    policy_version = canonical_agent_dynamics_config(world.model_configs.get("agent_dynamics"))[
        "policy_version"
    ]
    oriented = policy_version in _ORIENTED_POLICY_VERSIONS
    return sorted(
        facility.id
        for facility in world.facilities.values()
        if facility.zone_id == zone_id or (oriented and facility.kind in ORIENTATION_FACILITY_KINDS)
    )


def create_agent_dynamics_state(world: WorldState) -> dict[str, Any]:
    """Create bounded decision memory without changing legacy Agent encodings."""
    return {
        "schema_version": 1,
        "policy_version": canonical_agent_dynamics_config(
            world.model_configs.get("agent_dynamics")
        )["policy_version"],
        "agents": {
            agent_id: _new_agent_dynamics_record(world, agent_id)
            for agent_id in sorted(world.agents)
        },
    }


def validate_agent_dynamics_state(world: WorldState) -> None:
    """Reject malformed decision memory and references to absent Agents/facilities."""
    state = world.model_state.get(AGENT_DYNAMICS_STATE_KEY)
    if state is None:
        return
    if (
        not isinstance(state, dict)
        or set(state) != {"schema_version", "policy_version", "agents"}
        or state.get("schema_version") != 1
        or state.get("policy_version")
        != canonical_agent_dynamics_config(world.model_configs.get("agent_dynamics"))[
            "policy_version"
        ]
        or not isinstance(state.get("agents"), dict)
        or set(state["agents"]) != set(world.agents)
    ):
        raise ValueError("agent_dynamics_v1 has an invalid exact schema")
    for agent_id, record in state["agents"].items():
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "known_facility_ids",
                "last_action",
                "last_target_id",
                "last_decision_minute",
                "decision_due_minute",
                "decision_reason",
            }
            or not isinstance(record["known_facility_ids"], list)
            or len(record["known_facility_ids"]) != len(set(record["known_facility_ids"]))
            or set(record["known_facility_ids"]) - set(world.facilities)
            or not isinstance(record["decision_due_minute"], int)
            or record["decision_due_minute"] < 0
        ):
            raise ValueError(f"agent_dynamics_v1 record for {agent_id!r} is invalid")
        last = record["last_decision_minute"]
        if last is not None and (not isinstance(last, int) or isinstance(last, bool) or last < 0):
            raise ValueError(f"agent_dynamics_v1 decision minute for {agent_id!r} is invalid")


def record_proposal(world: WorldState, proposal: ActionProposal, *, target_id: str | None) -> None:
    """Persist the selected need-led intention while retaining full trace in artifacts."""
    state = _state(world)
    record = state["agents"].get(proposal.agent_id)
    if record is None:
        return
    record["last_action"] = proposal.action.value
    record["last_target_id"] = target_id
    record["last_decision_minute"] = proposal.minute_index
    record["decision_due_minute"] = proposal.next_decision_at
    record["decision_reason"] = proposal.trigger


def agent_knows_facility(world: WorldState, agent_id: str, facility_id: str) -> bool:
    """Apply explicit awareness only when the Scenario selected that model."""
    if not {"facility_awareness", "agent_dynamics"}.intersection(world.model_configs):
        # Older Scenarios had no knowledge gate. Keep their behavior independent
        # of whether a psychology policy happens to create autonomy memory.
        return True
    state = world.model_state.get(AGENT_DYNAMICS_STATE_KEY)
    if not isinstance(state, dict):
        return False
    record = state.get("agents", {}).get(agent_id)
    return record is not None and facility_id in record["known_facility_ids"]


def learn_facility(world: WorldState, agent_id: str, facility_id: str) -> None:
    """Record a facility and visible co-located facilities after actual arrival."""
    state = world.model_state.get(AGENT_DYNAMICS_STATE_KEY)
    facility = world.facilities.get(facility_id)
    if not isinstance(state, dict) or facility is None:
        return
    record = state["agents"].get(agent_id)
    if record is None:
        return
    config_document = world.model_configs.get("facility_awareness")
    config = (
        FacilityAwarenessConfig.model_validate(config_document)
        if isinstance(config_document, dict)
        else None
    )
    discovered_ids = {facility_id}
    if config is not None and config.discover_co_located_facilities_on_arrival:
        discovered_ids.update(
            item.id for item in world.facilities.values() if item.zone_id == facility.zone_id
        )
    record["known_facility_ids"] = sorted(set(record["known_facility_ids"]) | discovered_ids)


def announce_installed_facility(world: WorldState, facility_id: str) -> None:
    """Tell present Agents about a shared facility installed during the Run (v3 only).

    Agents admitted later already learn it through intake orientation.
    """
    state = world.model_state.get(AGENT_DYNAMICS_STATE_KEY)
    facility = world.facilities.get(facility_id)
    if (
        not isinstance(state, dict)
        or facility is None
        or facility.kind not in ORIENTATION_FACILITY_KINDS
        or canonical_agent_dynamics_config(world.model_configs.get("agent_dynamics"))[
            "policy_version"
        ]
        != ANNOUNCED_FACILITY_AGENT_DYNAMICS_POLICY_VERSION
    ):
        return
    for record in state["agents"].values():
        if facility_id not in record["known_facility_ids"]:
            record["known_facility_ids"] = sorted((*record["known_facility_ids"], facility_id))


def add_agent(world: WorldState, agent_id: str) -> None:
    """Initialize memory for a dynamically admitted Agent."""
    state = world.model_state.get(AGENT_DYNAMICS_STATE_KEY)
    agent = world.agents.get(agent_id)
    if state is None or agent is None:
        return
    state["agents"][agent_id] = _new_agent_dynamics_record(world, agent_id)


def remove_agent(world: WorldState, agent_id: str) -> dict[str, Any] | None:
    """Remove active memory and return its detached record for departure history."""
    state = world.model_state.get(AGENT_DYNAMICS_STATE_KEY)
    return None if state is None else state["agents"].pop(agent_id, None)


def _state(world: WorldState) -> dict[str, Any]:
    if AGENT_DYNAMICS_STATE_KEY not in world.model_state:
        world.model_state[AGENT_DYNAMICS_STATE_KEY] = create_agent_dynamics_state(world)
    return world.model_state[AGENT_DYNAMICS_STATE_KEY]


def _new_agent_dynamics_record(world: WorldState, agent_id: str) -> dict[str, Any]:
    agent = world.agents[agent_id]
    known_facility_ids = set(_initial_known_facility_ids(world, agent.zone_id))
    config_document = world.model_configs.get("facility_awareness")
    if isinstance(config_document, dict):
        config = FacilityAwarenessConfig.model_validate(config_document)
        known_facility_ids.update(config.initial_known_facility_ids)
    return {
        "known_facility_ids": sorted(known_facility_ids),
        "last_action": None,
        "last_target_id": None,
        "last_decision_minute": None,
        "decision_due_minute": agent.next_decision_at,
        "decision_reason": None,
    }
