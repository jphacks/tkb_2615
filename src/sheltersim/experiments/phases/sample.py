"""The sample phase: the contacts that become next minute's endogenous events."""

from __future__ import annotations

from sheltersim.core.engine import TraceLevel
from sheltersim.core.rng import KeyedRandom
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
from sheltersim.domain.social_network import PROXIMITY_CONTACT_SOURCE_ID
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.policies.behavior import WorldView


class SamplePhase:
    """Steps of TickPhase.SAMPLE_ENDOGENOUS_EVENTS, a mixin of OfflineSimulationController."""

    def _run_sample_phase(self, minute: int, rng: KeyedRandom) -> None:
        self._sample_information_contacts(minute, rng)
        self._sample_proximity_contacts(minute)

    def _sample_information_contacts(self, minute: int, rng: KeyedRandom) -> None:
        if self.information_policy is None:
            return
        # While no message exists, the Rust kernel needs only the people's zones. No
        # trace here: it records the contacts.
        result = (
            self.information_policy.native_update(
                self.world.agents,
                minute,
                self.world.model_state[INFORMATION_SPREAD_STATE_KEY],
                rng,
                zone_neighbors=self.zone_neighbors,
            )
            if self.artifacts.trace_level is TraceLevel.OFF
            else None
        )
        if result is None:
            result = self.information_policy.update(
                # Information policies read the people and the minute, never the audit hash.
                WorldView.from_world(
                    self.world,
                    include_state_hash=False,
                    person_cache=self._person_contexts,
                    environment_cache=self._zone_environments,
                ),
                self.world.model_state[INFORMATION_SPREAD_STATE_KEY],
                rng,
                zone_neighbors=self.zone_neighbors,
            )
        self.world.model_state[INFORMATION_SPREAD_STATE_KEY] = result.runtime_state
        for proposal in result.proposals:
            due_minute = minute + 1
            if due_minute >= self.world.duration_minutes:
                continue
            sequence = self.world.allocate_event_sequence()
            self.world.schedule_event(
                ScheduledEvent(
                    event_id=f"information-contact-{sequence}",
                    kind=EventKind.ENDOGENOUS,
                    due_minute=due_minute,
                    priority=40,
                    sequence=sequence,
                    event_type="information_contact_update",
                    payload={
                        "message_id": proposal.message_id,
                        "sender_agent_id": proposal.sender_agent_id,
                        "recipient_agent_id": proposal.recipient_agent_id,
                        "belief": proposal.belief.value,
                        "contact_basis": proposal.contact_basis,
                        "contact_minutes": proposal.contact_minutes,
                    },
                    source_id=proposal.message_source_id,
                    causation_id=proposal.causation_id,
                    provenance={
                        "source_type": "assumption",
                        "source_id": "ASM-INFORMATION-SPREAD-V1",
                        "policy_version": self.information_policy.policy_version,
                        "transmission_probability": proposal.probability,
                    },
                )
            )
        if self.artifacts.trace_level is not TraceLevel.OFF:
            self.artifacts.record_information_spread_trace(
                result.trace_document(
                    minute,
                    maximum_contacts=(
                        len(result.contacts)
                        if self.artifacts.trace_level is TraceLevel.FULL
                        else 20
                    ),
                )
            )

    def _sample_proximity_contacts(self, minute: int) -> None:
        """Schedule only reproducible same-zone physical proximity observations."""
        config = self.proximity_contact_config
        if (
            config is None
            or minute % config["sample_interval_minutes"] != 0
            or minute + 1 >= self.world.duration_minutes
        ):
            return
        for pair in self.proximity_policy.sample(self.world):
            due_minute = minute + 1
            sequence = self.world.allocate_event_sequence()
            self.world.schedule_event(
                ScheduledEvent(
                    event_id=f"proximity-contact-{sequence}",
                    kind=EventKind.ENDOGENOUS,
                    due_minute=due_minute,
                    priority=40,
                    sequence=sequence,
                    event_type="proximity_contact_update",
                    payload={
                        "first_agent_id": pair.first_agent_id,
                        "second_agent_id": pair.second_agent_id,
                        "zone_id": pair.zone_id,
                        "observed_minute": due_minute,
                        "contact_minutes": 0,
                        "distance_m": pair.distance_m,
                        "first_point": pair.first_point,
                        "second_point": pair.second_point,
                    },
                    source_id=PROXIMITY_CONTACT_SOURCE_ID,
                    provenance={
                        "source_type": "assumption",
                        "source_id": PROXIMITY_CONTACT_SOURCE_ID,
                        "policy_version": config["policy_version"],
                        "observed_minute": due_minute,
                    },
                )
            )
