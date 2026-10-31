"""The sample phase: the contacts that become next minute's endogenous events."""

from __future__ import annotations

from sheltersim.core.engine import TraceLevel
from sheltersim.core.rng import KeyedRandom
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
from sheltersim.domain.social_network import PROXIMITY_CONTACT_SOURCE_ID
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.policies.world_view import WorldView


class SamplePhase:
    """Steps of TickPhase.SAMPLE_ENDOGENOUS_EVENTS, a mixin of OfflineSimulationController."""

    def _run_sample_phase(self, minute: int, rng: KeyedRandom) -> None:
        self._sample_information_contacts(minute, rng)
        self._sample_proximity_contacts(minute)

    def _sample_information_contacts(self, minute: int, rng: KeyedRandom) -> None:
        """The Python rule; the kernel of rules/information.rs replaces this step (segments.py)."""
        if self.information_policy is None:
            return
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
            self._schedule_information_contact(
                minute,
                proposal.message_id,
                proposal.sender_agent_id,
                proposal.recipient_agent_id,
                proposal.belief.value,
                proposal.contact_basis,
                proposal.contact_minutes,
                proposal.probability,
                proposal.message_source_id,
                proposal.causation_id,
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

    def _schedule_information_contact(
        self,
        minute: int,
        message_id: str,
        sender_agent_id: str,
        recipient_agent_id: str,
        belief: str,
        contact_basis: str,
        contact_minutes: int,
        probability: float,
        message_source_id: str,
        causation_id: str,
    ) -> None:
        """Next minute's transfer of one claim, called by the Python rule and the kernel."""
        due_minute = minute + 1
        if due_minute >= self.world.duration_minutes:
            return
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
                    "message_id": message_id,
                    "sender_agent_id": sender_agent_id,
                    "recipient_agent_id": recipient_agent_id,
                    "belief": belief,
                    "contact_basis": contact_basis,
                    "contact_minutes": contact_minutes,
                },
                source_id=message_source_id,
                causation_id=causation_id,
                provenance={
                    "source_type": "assumption",
                    "source_id": "ASM-INFORMATION-SPREAD-V1",
                    "policy_version": self.information_policy.policy_version,
                    "transmission_probability": probability,
                },
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
