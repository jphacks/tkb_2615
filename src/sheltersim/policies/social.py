"""Sparse, deterministic contact-based stress propagation policy.

The policy is disabled by default. Its coefficients are scenario assumptions, not
calibrated estimates of real shelter behavior.
"""

from __future__ import annotations

import bisect
import json
import math
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping

from sheltersim.core.rng import KeyedRandom
from sheltersim.policies.behavior import WorldView
from sheltersim.policies.psychology import AgentStateDelta

_STATE_SCHEMA_VERSION = 1
_SOURCE_ID = "ASM-SOCIAL-PROPAGATION-V1"
SOCIAL_RUNTIME_STATE_KEY = "social_propagation_v1"


@dataclass(frozen=True)
class SocialPropagationParameters:
    """Bounded, auditable coefficients for one optional social stress model."""

    enabled: bool = False
    max_contacts_per_agent: int = 3
    minimum_contact_minutes: int = 5
    stress_spread_per_minute: float = 0.001
    natural_recovery_per_minute: float = 0.0005
    cooldown_minutes: int = 30
    max_pair_stress_increase: float = 0.05
    max_agent_stress_increase_per_minute: float = 0.05

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("social propagation enabled must be boolean")
        for name, value in (
            ("max_contacts_per_agent", self.max_contacts_per_agent),
            ("minimum_contact_minutes", self.minimum_contact_minutes),
            ("cooldown_minutes", self.cooldown_minutes),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
            minimum = 0 if name == "max_contacts_per_agent" else 1
            maximum = 16 if name == "max_contacts_per_agent" else 10_080
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        for name in (
            "stress_spread_per_minute",
            "natural_recovery_per_minute",
            "max_pair_stress_increase",
            "max_agent_stress_increase_per_minute",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not 0.0 <= value <= 0.25
            ):
                raise ValueError(f"{name} must be finite and between 0 and 0.25")

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any] | None) -> SocialPropagationParameters:
        """Validate a scenario's social-propagation model configuration."""
        if document is None:
            return cls()
        if not isinstance(document, Mapping):
            raise ValueError("social_propagation model config must be an object")
        allowed = set(cls.__dataclass_fields__)
        provenance_keys = {"source_type", "source_id"}
        unknown = set(document) - allowed - provenance_keys
        if unknown:
            raise ValueError(f"unknown social_propagation settings: {sorted(unknown)}")
        source_type = document.get("source_type")
        source_id = document.get("source_id")
        if source_type is not None and source_type != "assumption":
            raise ValueError("social_propagation source_type must be assumption")
        if source_id is not None and source_id != _SOURCE_ID:
            raise ValueError("social_propagation source_id does not match its model assumption")
        try:
            values = {key: value for key, value in document.items() if key in allowed}
            return cls(**values)
        except TypeError as exc:
            raise ValueError(f"invalid social_propagation settings: {exc}") from exc

    def provenance(self) -> dict[str, Any]:
        """Return source classification and the exact active coefficient values."""
        return {
            "source_type": "assumption",
            "source_id": _SOURCE_ID,
            "policy_version": "sparse-family-zone-contact-v1",
            "coefficients": asdict(self),
            "notes": (
                "Experimental coefficients for sensitivity analysis; not empirically "
                "calibrated or validated for real populations."
            ),
        }


@dataclass(frozen=True)
class SocialContactTrace:
    """One selected directed contact and the resulting bounded contribution."""

    recipient_id: str
    source_id: str
    contact_basis: str
    consecutive_contact_minutes: int
    cooldown_remaining_minutes: int
    stress_difference: float
    stress_contribution: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SocialPropagationResult:
    """Pure policy result; the controller applies deltas and stores runtime state."""

    deltas: tuple[AgentStateDelta, ...]
    runtime_state: dict[str, Any]
    contacts: tuple[SocialContactTrace, ...]
    contributing_contact_count: int
    total_stress_added: float
    policy_version: str = "sparse-family-zone-contact-v1"

    def trace_document(self, minute: int, *, maximum_contacts: int = 20) -> dict[str, Any]:
        """Compact per-minute trace: every influence aggregate plus a bounded edge sample."""
        if maximum_contacts < 0:
            raise ValueError("maximum_contacts cannot be negative")
        sample = self.contacts[:maximum_contacts]
        contributions = [item.to_dict() for item in self.contacts if item.stress_contribution != 0.0]
        return {
            "minute_index": minute,
            "policy_version": self.policy_version,
            "selected_contact_count": len(self.contacts),
            "contributing_contact_count": self.contributing_contact_count,
            "total_stress_added": self.total_stress_added,
            "truncated_contact_count": len(self.contacts) - len(sample),
            "contacts": [item.to_dict() for item in sample],
            "contributions": contributions,
        }


@dataclass(frozen=True)
class _ContactBucket:
    basis: str
    agent_ids: tuple[str, ...]
    includes_self: bool

    @property
    def eligible_count(self) -> int:
        return len(self.agent_ids) - int(self.includes_self)


class StressPropagationPolicy:
    """Sample a bounded sparse contact graph and propose explainable stress changes."""

    policy_version = "sparse-family-zone-contact-v1"

    def __init__(self, parameters: SocialPropagationParameters | None = None) -> None:
        self.parameters = parameters or SocialPropagationParameters()

    def update(
        self,
        view: WorldView,
        base_deltas: tuple[AgentStateDelta, ...],
        rng: KeyedRandom,
        *,
        zone_neighbors: Mapping[str, tuple[str, ...]],
        runtime_state: Mapping[str, Any] | None = None,
    ) -> SocialPropagationResult:
        """Return base psychology plus optional recovery and sparse social influence."""
        if not self.parameters.enabled:
            return SocialPropagationResult(base_deltas, dict(runtime_state or {}), (), 0, 0.0)
        if any(delta.minute_index != view.minute_index for delta in base_deltas):
            raise ValueError("base psychology deltas must match the WorldView minute")
        if {delta.agent_id for delta in base_deltas} != set(view.agents):
            raise ValueError("base psychology deltas must contain each current Agent exactly once")
        base_by_id = {delta.agent_id: delta for delta in base_deltas}

        previous_contacts, cooldowns = _read_runtime_state(runtime_state)
        group_members: dict[str, list[str]] = {}
        zone_members: dict[str, list[str]] = {}
        for agent in view.agents.values():
            if agent.group_id is not None:
                group_members.setdefault(agent.group_id, []).append(agent.id)
            zone_members.setdefault(agent.zone_id, []).append(agent.id)
        group_buckets = {key: tuple(sorted(value)) for key, value in group_members.items()}
        zone_buckets = {key: tuple(sorted(value)) for key, value in zone_members.items()}

        current_contacts: dict[str, dict[str, int]] = {}
        contacts: list[SocialContactTrace] = []
        additions: dict[str, float] = {agent_id: 0.0 for agent_id in view.agents}
        contributing = 0
        total_added = 0.0
        params = self.parameters
        for recipient_id in sorted(view.agents):
            recipient = view.agents[recipient_id]
            buckets = _contact_buckets(
                recipient,
                group_buckets=group_buckets,
                zone_buckets=zone_buckets,
                zone_neighbors=zone_neighbors,
            )
            selected = _select_contacts(
                recipient_id,
                view.minute_index,
                buckets,
                params.max_contacts_per_agent,
                rng,
            )
            for source_id, basis in selected:
                pair_key = _pair_key(recipient_id, source_id)
                previous = previous_contacts.get(pair_key)
                duration = (
                    previous["consecutive_minutes"] + 1
                    if previous is not None
                    and previous["last_seen_minute"] == view.minute_index - 1
                    else 1
                )
                cooldown_until = cooldowns.get(recipient_id, 0)
                source = view.agents[source_id]
                difference = max(0.0, source.stress - recipient.stress)
                contribution = 0.0
                if (
                    duration >= params.minimum_contact_minutes
                    and view.minute_index >= cooldown_until
                ):
                    contribution = min(
                        params.max_pair_stress_increase,
                        difference * params.stress_spread_per_minute * duration,
                    )
                    remaining_budget = max(
                        0.0,
                        min(
                            params.max_agent_stress_increase_per_minute - additions[recipient_id],
                            1.0
                            - max(
                                0.0,
                                base_by_id[recipient_id].stress
                                - params.natural_recovery_per_minute,
                            )
                            - additions[recipient_id],
                        ),
                    )
                    contribution = min(contribution, remaining_budget)
                    if contribution > 0.0:
                        cooldown_until = view.minute_index + params.cooldown_minutes
                        cooldowns[recipient_id] = cooldown_until
                        additions[recipient_id] += contribution
                        total_added += contribution
                        contributing += 1
                current_contacts[pair_key] = {
                    "last_seen_minute": view.minute_index,
                    "consecutive_minutes": duration,
                }
                contacts.append(
                    SocialContactTrace(
                        recipient_id=recipient_id,
                        source_id=source_id,
                        contact_basis=basis,
                        consecutive_contact_minutes=duration,
                        cooldown_remaining_minutes=max(0, cooldown_until - view.minute_index),
                        stress_difference=difference,
                        stress_contribution=contribution,
                    )
                )

        deltas: list[AgentStateDelta] = []
        for agent_id in sorted(base_by_id):
            delta = base_by_id[agent_id]
            stress = min(
                1.0,
                max(
                    0.0,
                    delta.stress - params.natural_recovery_per_minute + additions[agent_id],
                ),
            )
            deltas.append(replace(delta, stress=stress))

        contacts.sort(key=lambda item: (item.recipient_id, item.source_id, item.contact_basis))
        return SocialPropagationResult(
            deltas=tuple(deltas),
            runtime_state={
                "schema_version": _STATE_SCHEMA_VERSION,
                "contacts": current_contacts,
                "agent_cooldown_until": {
                    agent_id: due_minute
                    for agent_id, due_minute in sorted(cooldowns.items())
                    if due_minute > view.minute_index
                },
            },
            contacts=tuple(contacts),
            contributing_contact_count=contributing,
            total_stress_added=total_added,
        )


def _contact_buckets(
    agent: Any,
    *,
    group_buckets: Mapping[str, tuple[str, ...]],
    zone_buckets: Mapping[str, tuple[str, ...]],
    zone_neighbors: Mapping[str, tuple[str, ...]],
) -> tuple[_ContactBucket, ...]:
    buckets: list[_ContactBucket] = []
    if agent.group_id is not None:
        family = group_buckets.get(agent.group_id, [])
        if len(family) > 1:
            buckets.append(_ContactBucket("family", family, True))
    same_zone = zone_buckets.get(agent.zone_id, [])
    if len(same_zone) > 1:
        buckets.append(_ContactBucket("same_zone", same_zone, True))
    for zone_id in sorted(zone_neighbors.get(agent.zone_id, ())):
        neighbors = zone_buckets.get(zone_id, [])
        if neighbors:
            buckets.append(_ContactBucket("nearby_zone", neighbors, False))
    return tuple(buckets)


def _select_contacts(
    recipient_id: str,
    minute: int,
    buckets: tuple[_ContactBucket, ...],
    maximum: int,
    rng: KeyedRandom,
) -> tuple[tuple[str, str], ...]:
    """Choose at most k contacts in O(k × local-zone-degree), without pair matrices."""
    selected: list[tuple[str, str]] = []
    selected_ids: set[str] = set()
    if maximum == 0 or not buckets:
        return ()
    for slot in range(maximum):
        for attempt in range(maximum * 4):
            total = sum(bucket.eligible_count for bucket in buckets)
            if total <= 0:
                return tuple(selected)
            draw = rng.random("social-contact-bucket-v1", minute, recipient_id, slot, attempt)
            offset = min(total - 1, int(draw * total))
            chosen_bucket: _ContactBucket | None = None
            local_index = 0
            for bucket in buckets:
                if offset < bucket.eligible_count:
                    chosen_bucket = bucket
                    local_index = offset
                    break
                offset -= bucket.eligible_count
            if chosen_bucket is None:
                break
            if chosen_bucket.includes_self:
                self_index = bisect.bisect_left(chosen_bucket.agent_ids, recipient_id)
                if local_index >= self_index:
                    local_index += 1
            source_id = chosen_bucket.agent_ids[local_index]
            if source_id == recipient_id or source_id in selected_ids:
                continue
            selected_ids.add(source_id)
            selected.append((source_id, chosen_bucket.basis))
            break
        else:
            break
    return tuple(selected)


def _read_runtime_state(
    runtime_state: Mapping[str, Any] | None,
) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    if runtime_state is None:
        return {}, {}
    if (
        not isinstance(runtime_state, Mapping)
        or runtime_state.get("schema_version") != _STATE_SCHEMA_VERSION
    ):
        raise ValueError("unsupported social-propagation runtime state schema")
    raw_contacts = runtime_state.get("contacts")
    raw_cooldowns = runtime_state.get("agent_cooldown_until")
    if not isinstance(raw_contacts, Mapping) or not isinstance(raw_cooldowns, Mapping):
        raise ValueError("social-propagation contact and cooldown state must be objects")
    contacts: dict[str, dict[str, int]] = {}
    for key, value in raw_contacts.items():
        if not isinstance(key, str) or not isinstance(value, Mapping):
            raise ValueError("invalid social-propagation contact entry")
        numbers = {}
        for field in ("last_seen_minute", "consecutive_minutes"):
            number = value.get(field)
            if not isinstance(number, int) or isinstance(number, bool) or number < 0:
                raise ValueError(f"social-propagation {field} must be a non-negative integer")
            numbers[field] = number
        contacts[key] = numbers
    cooldowns: dict[str, int] = {}
    for agent_id, due_minute in raw_cooldowns.items():
        if (
            not isinstance(agent_id, str)
            or not isinstance(due_minute, int)
            or isinstance(due_minute, bool)
            or due_minute < 0
        ):
            raise ValueError("social-propagation cooldown entries must map Agent IDs to minutes")
        cooldowns[agent_id] = due_minute
    return contacts, cooldowns


def _pair_key(recipient_id: str, source_id: str) -> str:
    return json.dumps([recipient_id, source_id], ensure_ascii=False, separators=(",", ":"))
