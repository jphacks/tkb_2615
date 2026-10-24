"""Sparse, deterministic contact-based information diffusion and inquiry signals."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from sheltersim.core.rng import KeyedRandom
from sheltersim.domain.models import (
    AgentInformationState,
    InformationBeliefStatus,
    InformationSpreadConfig,
    TypedMessage,
)
from sheltersim.policies.behavior import WorldView

_BELIEF_RANK = {
    InformationBeliefStatus.UNKNOWN: 0,
    InformationBeliefStatus.HEARD: 1,
    InformationBeliefStatus.VERIFIED: 2,
    InformationBeliefStatus.REJECTED: 2,
}


@dataclass(frozen=True)
class InformationContactProposal:
    """One bounded contact that can transfer a new or corrected belief next tick."""

    message_id: str
    sender_agent_id: str
    recipient_agent_id: str
    belief: InformationBeliefStatus
    contact_basis: str
    contact_minutes: int
    probability: float
    message_source_id: str
    causation_id: str


@dataclass(frozen=True)
class InformationContactTrace:
    """Deterministic record of a sampled social contact and transfer decision."""

    sender_agent_id: str
    recipient_agent_id: str
    contact_basis: str
    contact_minutes: int
    message_id: str | None
    belief: str | None
    transmission_probability: float
    transmission_selected: bool
    message_source_id: str | None = None
    causation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InformationSpreadResult:
    """Updated persisted policy state plus proposed next-tick information events."""

    runtime_state: dict[str, Any]
    proposals: tuple[InformationContactProposal, ...]
    contacts: tuple[InformationContactTrace, ...]
    policy_version: str = "sparse-information-contact-v1"

    def trace_document(self, minute: int, *, maximum_contacts: int = 20) -> dict[str, Any]:
        if maximum_contacts < 0:
            raise ValueError("maximum_contacts cannot be negative")
        sample = self.contacts[:maximum_contacts]
        return {
            "minute_index": minute,
            "policy_version": self.policy_version,
            "selected_contact_count": len(self.contacts),
            "truncated_contact_count": len(self.contacts) - len(sample),
            "contacts": [contact.to_dict() for contact in sample],
            "proposed_transfer_count": len(self.proposals),
        }


@dataclass(frozen=True)
class _ContactBucket:
    basis: str
    agent_ids: tuple[str, ...]
    includes_self: bool

    @property
    def eligible_count(self) -> int:
        return len(self.agent_ids) - int(self.includes_self)


class InformationSpreadPolicy:
    """Transfer typed claims through sampled local contacts, never a global broadcast."""

    policy_version = "sparse-information-contact-v1"

    def __init__(self, config: InformationSpreadConfig) -> None:
        self.config = config

    def update(
        self,
        view: WorldView,
        runtime_state: Mapping[str, Any],
        rng: KeyedRandom,
        *,
        zone_neighbors: Mapping[str, tuple[str, ...]],
    ) -> InformationSpreadResult:
        """Sample a bounded directed contact set and propose evidence-linked transfers."""
        if not self.config.enabled:
            return InformationSpreadResult(deepcopy(dict(runtime_state)), (), ())
        state = deepcopy(dict(runtime_state))
        messages = {
            message_id: TypedMessage.from_dict(raw) for message_id, raw in state["messages"].items()
        }
        beliefs = state["beliefs"]
        query_pressure = state["query_pressure"]
        decay = self.config.query_pressure_decay_per_minute
        for agent_id in sorted(view.agents):
            query_pressure[agent_id] = max(0.0, query_pressure[agent_id] - decay)

        zones: dict[str, list[str]] = {}
        for agent in view.agents.values():
            zones.setdefault(agent.zone_id, []).append(agent.id)
        zone_buckets = {key: tuple(sorted(values)) for key, values in zones.items()}

        previous_contacts = state["contacts"]
        current_contacts: dict[str, dict[str, int]] = {}
        proposals: list[InformationContactProposal] = []
        traces: list[InformationContactTrace] = []
        planned_pairs: set[tuple[str, str]] = set()
        for recipient_id in sorted(view.agents):
            recipient = view.agents[recipient_id]
            buckets = _contact_buckets(
                recipient,
                zone_buckets=zone_buckets,
                zone_neighbors=zone_neighbors,
            )
            selected = _select_contacts(
                recipient_id,
                view.minute_index,
                buckets,
                self.config.max_contacts_per_agent,
                rng,
            )
            for sender_id, basis in selected:
                sender = view.agents[sender_id]
                if recipient.group_id is not None and recipient.group_id == sender.group_id:
                    basis = "family"
                pair_key = _pair_key(recipient_id, sender_id)
                previous = previous_contacts.get(pair_key)
                contact_minutes = (
                    previous["consecutive_minutes"] + 1
                    if previous is not None
                    and previous["last_seen_minute"] == view.minute_index - 1
                    else 1
                )
                current_contacts[pair_key] = {
                    "last_seen_minute": view.minute_index,
                    "consecutive_minutes": contact_minutes,
                }
                state["counters"]["contact_attempts"] += 1

                sender_beliefs = beliefs[sender_id]
                recipient_beliefs = beliefs[recipient_id]
                transferable = []
                for message_id, raw_belief in sender_beliefs.items():
                    sender_belief = AgentInformationState.from_dict(raw_belief)
                    receiver_raw = recipient_beliefs.get(message_id)
                    receiver_rank = (
                        _BELIEF_RANK[AgentInformationState.from_dict(receiver_raw).belief]
                        if receiver_raw is not None
                        else 0
                    )
                    if _BELIEF_RANK[sender_belief.belief] > receiver_rank:
                        transferable.append((message_id, sender_belief))

                if not transferable:
                    traces.append(
                        InformationContactTrace(
                            sender_agent_id=sender_id,
                            recipient_agent_id=recipient_id,
                            contact_basis=basis,
                            contact_minutes=contact_minutes,
                            message_id=None,
                            belief=None,
                            transmission_probability=0.0,
                            transmission_selected=False,
                        )
                    )
                    continue
                message_id, sender_belief = min(
                    transferable,
                    key=lambda item: (
                        -_BELIEF_RANK[item[1].belief],
                        messages[item[0]].time,
                        item[0],
                    ),
                )
                if contact_minutes < self.config.minimum_contact_minutes:
                    traces.append(
                        InformationContactTrace(
                            sender_agent_id=sender_id,
                            recipient_agent_id=recipient_id,
                            contact_basis=basis,
                            contact_minutes=contact_minutes,
                            message_id=message_id,
                            belief=sender_belief.belief.value,
                            transmission_probability=0.0,
                            transmission_selected=False,
                            message_source_id=messages[message_id].source_id,
                            causation_id=sender_belief.last_event_id,
                        )
                    )
                    continue
                correction = sender_belief.belief in (
                    InformationBeliefStatus.VERIFIED,
                    InformationBeliefStatus.REJECTED,
                )
                probability = self._transmission_probability(
                    recipient.trust,
                    basis,
                    contact_minutes,
                    correction=correction,
                )
                probability = min(
                    1.0,
                    probability * self._sender_sharing_factor(sender_belief, sender),
                )
                selected_transfer = (
                    rng.random(
                        "information-contact-transfer-v1",
                        view.minute_index,
                        recipient_id,
                        sender_id,
                        message_id,
                        sender_belief.belief.value,
                    )
                    < probability
                )
                traces.append(
                    InformationContactTrace(
                        sender_agent_id=sender_id,
                        recipient_agent_id=recipient_id,
                        contact_basis=basis,
                        contact_minutes=contact_minutes,
                        message_id=message_id,
                        belief=sender_belief.belief.value,
                        transmission_probability=probability,
                        transmission_selected=selected_transfer,
                        message_source_id=messages[message_id].source_id,
                        causation_id=sender_belief.last_event_id,
                    )
                )
                pair = (recipient_id, message_id)
                if selected_transfer and pair not in planned_pairs:
                    planned_pairs.add(pair)
                    message = messages[message_id]
                    proposals.append(
                        InformationContactProposal(
                            message_id=message_id,
                            sender_agent_id=sender_id,
                            recipient_agent_id=recipient_id,
                            belief=sender_belief.belief,
                            contact_basis=basis,
                            contact_minutes=contact_minutes,
                            probability=probability,
                            message_source_id=message.source_id,
                            causation_id=sender_belief.last_event_id,
                        )
                    )

        state["contacts"] = current_contacts
        proposals.sort(
            key=lambda item: (
                item.recipient_agent_id,
                item.sender_agent_id,
                item.message_id,
            )
        )
        traces.sort(
            key=lambda item: (
                item.recipient_agent_id,
                item.sender_agent_id,
                item.message_id or "",
            )
        )
        return InformationSpreadResult(state, tuple(proposals), tuple(traces))

    def _sender_sharing_factor(self, belief: AgentInformationState, sender: Any) -> float:
        """Return a multiplicative propensity; the legacy model shares as before."""
        return 1.0

    def record_support_request(
        self,
        runtime_state: dict[str, Any],
        *,
        agent_id: str,
        request_id: str,
        minute: int,
    ) -> None:
        """Tie a real staff request to the unresolved information that raised its signal."""
        message_ids = sorted(
            message_id
            for message_id, belief in runtime_state["beliefs"][agent_id].items()
            if belief["belief"] == InformationBeliefStatus.HEARD.value
        )
        if not message_ids:
            return
        runtime_state["support_requests"].append(
            {
                "request_id": request_id,
                "agent_id": agent_id,
                "minute": minute,
                "message_ids": message_ids,
            }
        )
        runtime_state["counters"]["support_requests"] += 1
        runtime_state["query_pressure"][agent_id] = 0.0

    def _transmission_probability(
        self,
        trust: float,
        contact_basis: str,
        contact_minutes: int,
        *,
        correction: bool,
    ) -> float:
        duration_factor = min(
            1.0,
            contact_minutes / self.config.contact_duration_scale_minutes,
        )
        trust_factor = 1.0 + self.config.trust_sensitivity * (2.0 * trust - 1.0)
        basis_factor = {
            "family": self.config.family_contact_multiplier,
            "same_zone": 1.0,
            "adjacent_zone": self.config.adjacent_zone_contact_multiplier,
        }[contact_basis]
        correction_factor = self.config.correction_transmission_multiplier if correction else 1.0
        return min(
            1.0,
            self.config.base_transmission_probability
            * duration_factor
            * trust_factor
            * basis_factor
            * correction_factor,
        )


class EvidenceAwareInformationPolicy(InformationSpreadPolicy):
    """Separate exposure, belief, sharing, and forgetting as explicit processes."""

    policy_version = "evidence-aware-rumor-v1"

    def update(
        self,
        view: WorldView,
        runtime_state: Mapping[str, Any],
        rng: KeyedRandom,
        *,
        zone_neighbors: Mapping[str, tuple[str, ...]],
    ) -> InformationSpreadResult:
        state = deepcopy(dict(runtime_state))
        decay = self.config.share_propensity_decay_per_minute
        forget_probability = -math.expm1(-self.config.forgetting_rate_per_minute)
        for agent_id, agent_beliefs in state["beliefs"].items():
            for message_id, raw in list(agent_beliefs.items()):
                belief = AgentInformationState.from_dict(raw)
                propensity = max(0.0, belief.share_propensity - decay)
                forget_draw = rng.random(
                    "information-forgetting-v1", view.minute_index, agent_id, message_id
                )
                if forget_draw < forget_probability:
                    updated = AgentInformationState(
                        message_id=message_id, belief=InformationBeliefStatus.UNKNOWN,
                        source=belief.source, last_contact=belief.last_contact,
                        last_event_id=belief.last_event_id, exposed=True,
                        belief_strength=belief.belief_strength, share_propensity=0.0,
                        correction_exposed=belief.correction_exposed,
                        correction_accepted=belief.correction_accepted,
                    )
                else:
                    updated = AgentInformationState(
                        **{**belief.to_dict(), "share_propensity": propensity}
                    )
                agent_beliefs[message_id] = updated.to_dict()
        result = super().update(view, state, rng, zone_neighbors=zone_neighbors)
        return InformationSpreadResult(
            result.runtime_state, result.proposals, result.contacts, self.policy_version
        )

    def _sender_sharing_factor(self, belief: AgentInformationState, sender: Any) -> float:
        distress_multiplier = 1.0 + self.config.distress_sharing_sensitivity * sender.stress
        return min(1.0, belief.share_propensity * distress_multiplier)


def _contact_buckets(
    agent: Any,
    *,
    zone_buckets: Mapping[str, tuple[str, ...]],
    zone_neighbors: Mapping[str, tuple[str, ...]],
) -> tuple[_ContactBucket, ...]:
    """Build local same/adjacent-zone buckets; family changes the probability only."""
    buckets: list[_ContactBucket] = []
    same_zone = zone_buckets.get(agent.zone_id, ())
    if len(same_zone) > 1:
        buckets.append(_ContactBucket("same_zone", same_zone, True))
    for zone_id in sorted(zone_neighbors.get(agent.zone_id, ())):
        neighbors = zone_buckets.get(zone_id, ())
        if neighbors:
            buckets.append(_ContactBucket("adjacent_zone", neighbors, False))
    return tuple(buckets)


def _select_contacts(
    recipient_id: str,
    minute: int,
    buckets: tuple[_ContactBucket, ...],
    maximum: int,
    rng: KeyedRandom,
) -> tuple[tuple[str, str], ...]:
    """Sample at most k partners without materializing all local Agent pairs."""
    if maximum == 0 or not buckets:
        return ()
    selected: list[tuple[str, str]] = []
    selected_ids: set[str] = set()
    for slot in range(maximum):
        for attempt in range(maximum * 4):
            total = sum(bucket.eligible_count for bucket in buckets)
            if total <= 0:
                return tuple(selected)
            draw = rng.random("information-contact-partner-v1", minute, recipient_id, slot, attempt)
            offset = min(total - 1, int(draw * total))
            chosen: _ContactBucket | None = None
            index = 0
            for bucket in buckets:
                if offset < bucket.eligible_count:
                    chosen = bucket
                    index = offset
                    break
                offset -= bucket.eligible_count
            if chosen is None:
                break
            if chosen.includes_self:
                self_index = _lower_bound(chosen.agent_ids, recipient_id)
                if index >= self_index:
                    index += 1
            source_id = chosen.agent_ids[index]
            if source_id == recipient_id or source_id in selected_ids:
                continue
            selected_ids.add(source_id)
            selected.append((source_id, chosen.basis))
            break
    return tuple(selected)


def _lower_bound(values: tuple[str, ...], target: str) -> int:
    low, high = 0, len(values)
    while low < high:
        middle = (low + high) // 2
        if values[middle] < target:
            low = middle + 1
        else:
            high = middle
    return low


def _pair_key(recipient_id: str, source_id: str) -> str:
    return json.dumps([recipient_id, source_id], ensure_ascii=False, separators=(",", ":"))
