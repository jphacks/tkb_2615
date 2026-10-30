"""Application handlers and deterministic dispatch for due events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, Protocol

from sheltersim.contracts.operations import NumericStateChange
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.care_receipts import (
    care_receipt_for_request,
    make_care_transition_record,
    make_facility_recovery_record,
    make_staff_operation_record,
    make_staff_support_continuity_record,
)
from sheltersim.domain.facilities import ToiletServiceManager, complete_toilet_service
from sheltersim.domain.information import (
    INFORMATION_SPREAD_STATE_KEY,
    upgrade_information_runtime_state,
)
from sheltersim.domain.infrastructure import (
    INFRASTRUCTURE_STATE_KEY,
    ScenarioInfrastructureSpec,
    active_facility_outage,
    dependent_facilities,
    effective_facility_capacities,
)
from sheltersim.domain.models import (
    ActiveTask,
    Activity,
    AgentInformationState,
    InformationBeliefStatus,
    InformationSpreadConfig,
    InformationTruthStatus,
    SupportRequestStatus,
    TypedMessage,
)
from sheltersim.domain.movement import (
    MOVEMENT_STATE_KEY,
    agent_is_at_facility_service_point,
    agent_is_at_queue_slot,
    agent_is_at_queue_trip_destination,
    facility_approach_route,
    finish_movement,
    start_movement,
)
from sheltersim.domain.operational import OPERATIONAL_EVENTS_KEY
from sheltersim.domain.resource_service import ResourceServiceManager
from sheltersim.domain.resources import ResourceLedgerError, apply_resource_operation
from sheltersim.domain.social_dialogue import validate_social_state_context
from sheltersim.domain.staff import (
    STAFF_SUPPORT_POLICY_VERSION,
    complete_support_task,
    timeout_support_request,
)
from sheltersim.events.effects import (
    effect_event_guard_or_suppress,
    has_active_facility_modifier,
)
from sheltersim.events.models import (
    EventApplicationError,
    EventKind,
    EventLogRecord,
    EventStatus,
    EventSuppressed,
    ScheduledEvent,
)

if TYPE_CHECKING:
    from sheltersim.core.engine import TickPhase


class EventHandler(Protocol):
    """Apply one event atomically, raising expected outcomes before mutation."""

    def apply(
        self, world: WorldState, event: ScheduledEvent
    ) -> tuple[EventLogRecord, ...] | None: ...


def _current_household_id(world: WorldState, agent_id: str) -> str | None:
    """Use mutable presence household assignments while retaining profile fallback."""
    from sheltersim.domain.population_lifecycle import current_household_id

    return current_household_id(world, agent_id)


def _social_payload_fields(payload: Mapping[str, Any]) -> set[str]:
    return set(payload) - {"conversation_state_context"}


class EventDispatcher:
    """Dispatch each due event once during the engine's first tick phase."""

    def __init__(self, handlers: dict[str, EventHandler] | None = None) -> None:
        self.handlers: dict[str, EventHandler] = dict(handlers or {})

    def register(self, event_type: str, handler: EventHandler) -> None:
        """Register or replace the handler for a stable event type."""
        if not event_type or event_type in self.handlers:
            raise ValueError(f"event handler already registered or invalid: {event_type!r}")
        self.handlers[event_type] = handler

    def __call__(self, phase: TickPhase, world: WorldState, minute: int, rng: object) -> None:
        """Use as a TickEngine phase handler; other phases remain untouched."""
        if phase == "dispatch_due_events":
            self.dispatch_due(world, minute)

    def dispatch_due(
        self,
        world: WorldState,
        minute: int,
        *,
        after_event: Callable[[tuple[EventLogRecord, ...]], None] | None = None,
    ) -> tuple[EventLogRecord, ...]:
        """Apply due events in order; observe each committed outcome before the next."""
        outcomes: list[EventLogRecord] = []
        for event in world.event_queue.due(minute):
            current = world.event_queue.find(event.event_id)
            if current is None:
                # A preceding event in this same due batch may cancel this
                # snapshot entry (departure, shift interruption, or replacement).
                # The cancellation path already emitted the single audit record.
                continue
            if current is not event and current.to_dict() != event.to_dict():
                # A preceding due event may have replaced this item with a new schedule.
                continue
            handler = self.handlers.get(event.event_type)
            additional_records: tuple[EventLogRecord, ...] = ()
            if handler is None:
                status = EventStatus.FAILED
                detail = f"no handler registered for event type {event.event_type!r}"
            else:
                try:
                    effect_event_guard_or_suppress(world, event)
                    additional_records = handler.apply(world, event) or ()
                except EventSuppressed as exc:
                    status = EventStatus.SUPPRESSED
                    detail = str(exc)
                except EventApplicationError as exc:
                    status = EventStatus.FAILED
                    detail = str(exc)
                else:
                    status = EventStatus.APPLIED
                    detail = None
                    if event.event_type in {
                        "information_contact_update",
                        "social_interaction",
                        "information_correction",
                        "proximity_contact_update",
                    }:
                        from sheltersim.domain.body_state import record_social_event
                        from sheltersim.domain.social_network import (
                            SOCIAL_NETWORK_STATE_KEY,
                            observe_event,
                        )

                        record_social_event(world, event, minute)
                        if SOCIAL_NETWORK_STATE_KEY in world.model_state:
                            observe_event(world, event, minute)

            if world.event_queue.find(event.event_id) is not None:
                world.event_queue.remove(event.event_id)
            record = world.event_log.find(event.event_id)
            if record is not None:
                if (
                    record.event is not event and record.event.to_dict() != event.to_dict()
                ) or record.status is not status:
                    raise ValueError(
                        "transactional event record disagrees with its dispatch result"
                    )
            else:
                record = EventLogRecord(
                    event=event,
                    realized_minute=minute,
                    status=status,
                    detail=detail,
                )
                world.event_log.append(record)
            event_outcomes = [record]
            outcomes.append(record)
            for additional in additional_records:
                existing = world.event_log.find(additional.event.event_id)
                if existing is not None:
                    if (
                        existing.event.to_dict() != additional.event.to_dict()
                        or existing.status is not additional.status
                    ):
                        raise ValueError(
                            "transactional follow-up record disagrees with dispatch result"
                        )
                    outcomes.append(existing)
                    event_outcomes.append(existing)
                else:
                    world.event_log.append(additional)
                    outcomes.append(additional)
                    event_outcomes.append(additional)
            if after_event is not None:
                after_event(tuple(event_outcomes))
        return tuple(outcomes)


@dataclass(frozen=True)
class ProximityContactHandler:
    """Accept a sampled physical encounter without asserting a conversation."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        import math

        from sheltersim.domain.social_network import (
            PROXIMITY_CONTACT_SOURCE_ID,
            canonical_proximity_contact_config,
        )

        config = canonical_proximity_contact_config(world.model_configs.get("social_proximity"))
        expected_provenance = {
            "source_type",
            "source_id",
            "policy_version",
            "observed_minute",
        }
        payload = event.payload
        if (
            config is None
            or event.kind is not EventKind.ENDOGENOUS
            or event.due_minute != world.minute_index
            or event.source_id != PROXIMITY_CONTACT_SOURCE_ID
            or set(event.provenance) != expected_provenance
            or event.provenance.get("source_type") != "assumption"
            or event.provenance.get("source_id") != PROXIMITY_CONTACT_SOURCE_ID
            or event.provenance.get("policy_version") != config["policy_version"]
        ):
            raise EventApplicationError("proximity contact provenance or time is invalid")
        if set(payload) != {
            "first_agent_id",
            "second_agent_id",
            "zone_id",
            "observed_minute",
            "contact_minutes",
            "distance_m",
            "first_point",
            "second_point",
        }:
            raise EventApplicationError("proximity contact payload fields do not match")
        first_id, second_id = payload["first_agent_id"], payload["second_agent_id"]
        observed_minute = payload["observed_minute"]
        zone_id = payload["zone_id"]
        duration = payload["contact_minutes"]
        distance = payload["distance_m"]
        if (
            not isinstance(first_id, str)
            or not first_id
            or not isinstance(second_id, str)
            or not second_id
            or first_id == second_id
            or first_id not in world.agents
            or second_id not in world.agents
            or not isinstance(zone_id, str)
            or zone_id not in world.zones
            or type(observed_minute) is not int
            or observed_minute != event.due_minute
            or type(event.provenance.get("observed_minute")) is not int
            or event.provenance.get("observed_minute") != observed_minute
            or type(duration) is not int
            or duration != 0
            or isinstance(distance, bool)
            or not isinstance(distance, (int, float))
            or not math.isfinite(float(distance))
            or distance < 0
            or distance > config["max_distance_m"]
        ):
            raise EventApplicationError("proximity contact participants or measurement are invalid")
        points = (payload["first_point"], payload["second_point"])
        if any(
            not isinstance(point, Mapping)
            or set(point) != {"x", "y"}
            or any(
                isinstance(point[key], bool)
                or not isinstance(point[key], (int, float))
                or not math.isfinite(float(point[key]))
                for key in ("x", "y")
            )
            for point in points
        ):
            raise EventApplicationError("proximity contact coordinates are invalid")
        measured = math.hypot(
            float(points[1]["x"]) - float(points[0]["x"]),
            float(points[1]["y"]) - float(points[0]["y"]),
        )
        if not math.isclose(float(distance), measured, rel_tol=1e-9, abs_tol=1e-9):
            raise EventApplicationError("proximity contact distance does not match coordinates")
        movement = world.model_state.get(MOVEMENT_STATE_KEY)
        positions = movement.get("positions", {}) if isinstance(movement, dict) else {}
        for agent_id, point in ((first_id, points[0]), (second_id, points[1])):
            position = positions.get(agent_id) if isinstance(positions, dict) else None
            if (
                not isinstance(position, dict)
                or position.get("position_kind") != "physical"
                or position.get("zone_id") != zone_id
                or position.get("point") != point
            ):
                raise EventSuppressed("proximity contact sample is stale")


@dataclass(frozen=True)
class InformationRumorHandler:
    """Seed one local Agent with a typed, provenance-linked claim."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> tuple[EventLogRecord, ...] | None:
        state = _information_runtime_state(world)
        from sheltersim.contracts.policies import selected_models

        if set(event.payload) != {"message_id", "truth_status", "origin_agent_id"}:
            raise EventApplicationError("information_rumor payload fields do not match")
        if event.source_id is None:
            raise EventApplicationError("information_rumor requires a source_id")
        message_id = event.payload.get("message_id")
        origin_agent_id = event.payload.get("origin_agent_id")
        if not isinstance(message_id, str) or not isinstance(origin_agent_id, str):
            raise EventApplicationError("information_rumor requires stable message and Agent IDs")
        try:
            truth_status = InformationTruthStatus(event.payload.get("truth_status"))
        except (TypeError, ValueError) as exc:
            raise EventApplicationError("information_rumor truth_status is invalid") from exc
        if message_id in state["messages"]:
            raise EventApplicationError("information message ID already exists")
        if len(state["messages"]) >= 1024:
            raise EventApplicationError("information-spread message limit was reached")
        agent = world.agents.get(origin_agent_id)
        if agent is None:
            raise EventApplicationError("information_rumor references an unknown Agent")
        message = TypedMessage(
            id=message_id,
            truth_status=truth_status,
            source_id=event.source_id,
            zone_id=agent.zone_id,
            time=event.due_minute,
            origin_event_id=event.event_id,
            origin_agent_id=origin_agent_id,
        )
        state["messages"][message.id] = message.to_dict()
        config = InformationSpreadConfig.from_mapping(world.model_configs["information_spread"])
        belief_strength = 0.5
        if selected_models(world.model_configs)["information"] == "evidence-aware-rumor-v1":
            belief_strength = _sample_claim_belief_strength(
                world,
                config,
                agent_id=origin_agent_id,
                source_id=message.source_id,
                event_id=event.event_id,
                minute=event.due_minute,
                message_id=message.id,
            )
        state["beliefs"][origin_agent_id][message.id] = AgentInformationState(
            message_id=message.id,
            belief=InformationBeliefStatus.HEARD,
            source=origin_agent_id,
            last_contact=event.due_minute,
            last_event_id=event.event_id,
            belief_strength=belief_strength,
            ever_misbelieved=_claim_is_misbelieved(message.truth_status, belief_strength),
        ).to_dict()
        state["query_pressure"][origin_agent_id] = min(
            1.0,
            state["query_pressure"][origin_agent_id] + config.query_pressure_per_claim,
        )


@dataclass(frozen=True)
class InformationCorrectionHandler:
    """Publish a zone-scoped, source-linked correction to one existing claim."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        state = _information_runtime_state(world)
        from sheltersim.contracts.policies import selected_models

        if set(event.payload) != {"message_id", "zone_id", "belief"}:
            raise EventApplicationError("information_correction payload fields do not match")
        if event.source_id is None:
            raise EventApplicationError("information_correction requires a source_id")
        message_id = event.payload.get("message_id")
        zone_id = event.payload.get("zone_id")
        if not isinstance(message_id, str) or not isinstance(zone_id, str):
            raise EventApplicationError(
                "information_correction requires stable message and Zone IDs"
            )
        try:
            belief = InformationBeliefStatus(event.payload.get("belief"))
        except (TypeError, ValueError) as exc:
            raise EventApplicationError("information_correction belief is invalid") from exc
        if belief not in (InformationBeliefStatus.VERIFIED, InformationBeliefStatus.REJECTED):
            raise EventApplicationError("information_correction must verify or reject a claim")
        if zone_id not in world.zones:
            raise EventApplicationError("information_correction references an unknown Zone")
        raw_message = state["messages"].get(message_id)
        if raw_message is None:
            raise EventApplicationError("information_correction references an unknown message")
        message = TypedMessage.from_dict(raw_message)
        if event.causation_id != message.origin_event_id:
            raise EventApplicationError("information_correction must reference its rumor event")
        truth_belief = {
            InformationTruthStatus.TRUE: InformationBeliefStatus.VERIFIED,
            InformationTruthStatus.FALSE: InformationBeliefStatus.REJECTED,
        }.get(message.truth_status)
        if truth_belief is not None and truth_belief is not belief:
            raise EventApplicationError("information correction conflicts with message truth")

        config = InformationSpreadConfig.from_mapping(world.model_configs["information_spread"])
        evidence_aware = (
            selected_models(world.model_configs)["information"] == "evidence-aware-rumor-v1"
        )
        source_reliability = config.correction_source_reliability_by_id.get(
            event.source_id, config.default_source_reliability
        )
        targets = sorted(agent.id for agent in world.agents.values() if agent.zone_id == zone_id)
        changes: list[str] = []
        acceptances: dict[str, bool] = {}
        for agent_id in targets:
            previous = state["beliefs"][agent_id].get(message_id)
            prior_state = (
                AgentInformationState.from_dict(previous) if previous is not None else None
            )
            if evidence_aware:
                trust = world.agents[agent_id].trust
                probability = min(
                    0.95,
                    max(
                        0.0,
                        config.correction_acceptance_probability
                        + 2.0 * config.correction_trust_sensitivity * (trust - 0.5)
                        + 2.0 * config.correction_source_sensitivity * (source_reliability - 0.5),
                    ),
                )
                acceptances[agent_id] = (
                    CounterRNG(world.seed).random(
                        "correction-acceptance-v1",
                        event.due_minute,
                        event.event_id,
                        agent_id,
                        message_id,
                    )
                    < probability
                )
            if previous is not None:
                old_belief = prior_state.belief
                if old_belief in (
                    InformationBeliefStatus.VERIFIED,
                    InformationBeliefStatus.REJECTED,
                ):
                    if old_belief is not belief:
                        raise EventApplicationError("conflicting verified information state")
                    if not evidence_aware:
                        continue
            changes.append(agent_id)
        if message.truth_status is InformationTruthStatus.UNKNOWN:
            truth_status = {
                InformationBeliefStatus.VERIFIED: InformationTruthStatus.TRUE,
                InformationBeliefStatus.REJECTED: InformationTruthStatus.FALSE,
            }[belief]
            state["messages"][message_id] = replace(message, truth_status=truth_status).to_dict()
        for agent_id in changes:
            previous = state["beliefs"][agent_id].get(message_id)
            previous_state = (
                AgentInformationState.from_dict(previous) if previous is not None else None
            )
            accepted = acceptances.get(agent_id, True)
            state["beliefs"][agent_id][message_id] = AgentInformationState(
                message_id=message_id,
                belief=(
                    belief
                    if accepted
                    else previous_state.belief
                    if previous_state is not None
                    else InformationBeliefStatus.HEARD
                ),
                source=event.source_id,
                last_contact=event.due_minute,
                last_event_id=event.event_id,
                exposed=True,
                belief_strength=(
                    (1.0 if belief is InformationBeliefStatus.VERIFIED else 0.0)
                    if accepted
                    else previous_state.belief_strength
                    if previous_state is not None
                    else 0.5
                ),
                share_propensity=(
                    previous_state.share_propensity if previous_state is not None else 0.5
                ),
                correction_exposed=True,
                correction_accepted=accepted
                or bool(previous_state is not None and previous_state.correction_accepted),
                last_correction_accepted=accepted,
                ever_misbelieved=(
                    bool(previous_state and previous_state.ever_misbelieved)
                    or (
                        not accepted
                        and _claim_is_misbelieved(
                            message.truth_status,
                            previous_state.belief_strength if previous_state else 0.5,
                        )
                    )
                ),
                ever_forgotten=bool(previous_state and previous_state.ever_forgotten),
            ).to_dict()
            if accepted:
                state["query_pressure"][agent_id] = max(
                    0.0,
                    state["query_pressure"][agent_id] - config.query_pressure_relief_per_correction,
                )
                state["counters"]["correction_deliveries"] += 1


@dataclass(frozen=True)
class InformationContactHandler:
    """Apply one sampled, still-local contact at the next tick boundary."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        state = _information_runtime_state(world)
        expected = {
            "message_id",
            "sender_agent_id",
            "recipient_agent_id",
            "belief",
            "contact_basis",
            "contact_minutes",
        }
        if set(event.payload) != expected:
            raise EventApplicationError("information_contact_update payload fields do not match")
        if event.kind is not EventKind.ENDOGENOUS:
            raise EventApplicationError("information contact updates must be endogenous")
        if event.due_minute != world.minute_index:
            raise EventApplicationError("information contact update must run at its due minute")
        from sheltersim.contracts.policies import selected_models

        if (
            event.provenance.get("source_type") != "assumption"
            or event.provenance.get("source_id") != "ASM-INFORMATION-SPREAD-V1"
            or event.provenance.get("policy_version")
            != selected_models(world.model_configs)["information"]
        ):
            raise EventApplicationError(
                "information contact provenance is missing its assumption ID"
            )
        message_id = event.payload.get("message_id")
        sender_id = event.payload.get("sender_agent_id")
        recipient_id = event.payload.get("recipient_agent_id")
        if not all(isinstance(value, str) for value in (message_id, sender_id, recipient_id)):
            raise EventApplicationError("information contact requires stable claim and Agent IDs")
        if sender_id == recipient_id:
            raise EventApplicationError("information contact requires two different Agents")
        sender = world.agents.get(sender_id)
        recipient = world.agents.get(recipient_id)
        raw_message = state["messages"].get(message_id)
        if sender is None or recipient is None or raw_message is None:
            raise EventApplicationError("information contact references unknown state")
        message = TypedMessage.from_dict(raw_message)
        sender_raw = state["beliefs"][sender_id].get(message_id)
        if sender_raw is None:
            raise EventSuppressed("contact sender no longer knows the message")
        sender_belief = AgentInformationState.from_dict(sender_raw)
        try:
            belief = InformationBeliefStatus(event.payload.get("belief"))
        except (TypeError, ValueError) as exc:
            raise EventApplicationError("information contact belief is invalid") from exc
        if sender_belief.belief is not belief:
            raise EventSuppressed("contact sender belief changed before transmission")
        basis = _current_information_contact_basis(world, sender_id, recipient_id)
        if basis is None:
            raise EventSuppressed("Agents are no longer in a modeled contact zone")
        if event.payload.get("contact_basis") != basis:
            raise EventSuppressed("sampled contact basis is no longer current")
        contact_minutes = event.payload.get("contact_minutes")
        config = InformationSpreadConfig.from_mapping(world.model_configs["information_spread"])
        transmission_probability = event.provenance.get("transmission_probability")
        if (
            not isinstance(contact_minutes, int)
            or isinstance(contact_minutes, bool)
            or contact_minutes < config.minimum_contact_minutes
        ):
            raise EventApplicationError(
                "information contact duration is below its configured minimum"
            )
        if (
            not isinstance(transmission_probability, (int, float))
            or isinstance(transmission_probability, bool)
            or not 0.0 < transmission_probability <= 1.0
        ):
            raise EventApplicationError("information contact probability is invalid")
        if (
            event.source_id != message.source_id
            or event.causation_id != sender_belief.last_event_id
        ):
            raise EventApplicationError(
                "information contact provenance does not match sender state"
            )
        truth_belief = {
            InformationTruthStatus.TRUE: InformationBeliefStatus.VERIFIED,
            InformationTruthStatus.FALSE: InformationBeliefStatus.REJECTED,
        }.get(message.truth_status)
        if belief in (InformationBeliefStatus.VERIFIED, InformationBeliefStatus.REJECTED):
            if truth_belief is not belief:
                raise EventApplicationError("transmitted correction conflicts with message truth")

        evidence_aware = (
            selected_models(world.model_configs)["information"] == "evidence-aware-rumor-v1"
        )
        previous = state["beliefs"][recipient_id].get(message_id)
        if previous is not None:
            previous_belief = AgentInformationState.from_dict(previous).belief
            rank = {
                InformationBeliefStatus.UNKNOWN: 0,
                InformationBeliefStatus.HEARD: 1,
                InformationBeliefStatus.VERIFIED: 2,
                InformationBeliefStatus.REJECTED: 2,
            }
            if rank[previous_belief] >= rank[belief] and not (
                evidence_aware
                and belief in (InformationBeliefStatus.VERIFIED, InformationBeliefStatus.REJECTED)
            ):
                raise EventSuppressed("recipient already has an equally strong belief")
        accepted = True
        if evidence_aware and belief in (
            InformationBeliefStatus.VERIFIED,
            InformationBeliefStatus.REJECTED,
        ):
            sender_reliability = config.correction_source_reliability_by_id.get(
                sender_belief.source, config.default_source_reliability
            )
            probability = min(
                0.95,
                max(
                    0.0,
                    config.correction_acceptance_probability
                    + 2.0 * config.correction_trust_sensitivity * (recipient.trust - 0.5)
                    + 2.0 * config.correction_source_sensitivity * (sender_reliability - 0.5),
                ),
            )
            accepted = (
                CounterRNG(world.seed).random(
                    "contact-correction-acceptance-v1",
                    event.due_minute,
                    event.event_id,
                    recipient_id,
                    message_id,
                )
                < probability
            )
        previous_state = AgentInformationState.from_dict(previous) if previous is not None else None
        belief_strength = (
            _sample_claim_belief_strength(
                world,
                config,
                agent_id=recipient_id,
                source_id=message.source_id,
                event_id=event.event_id,
                minute=event.due_minute,
                message_id=message_id,
            )
            if evidence_aware and belief is InformationBeliefStatus.HEARD
            else previous_state.belief_strength
            if previous_state
            else 0.5
        )
        state["beliefs"][recipient_id][message_id] = AgentInformationState(
            message_id=message_id,
            belief=(
                belief
                if accepted
                else previous_state.belief
                if previous_state
                else InformationBeliefStatus.HEARD
            ),
            source=sender_id,
            last_contact=event.due_minute,
            last_event_id=event.event_id,
            exposed=True,
            belief_strength=(
                (1.0 if belief is InformationBeliefStatus.VERIFIED else 0.0)
                if accepted
                and belief in (InformationBeliefStatus.VERIFIED, InformationBeliefStatus.REJECTED)
                else belief_strength
            ),
            share_propensity=(previous_state.share_propensity if previous_state else 0.5),
            correction_exposed=(
                belief in (InformationBeliefStatus.VERIFIED, InformationBeliefStatus.REJECTED)
                or bool(previous_state and previous_state.correction_exposed)
            ),
            correction_accepted=(
                accepted
                and belief in (InformationBeliefStatus.VERIFIED, InformationBeliefStatus.REJECTED)
                or bool(previous_state and previous_state.correction_accepted)
            ),
            last_correction_accepted=(
                accepted
                if belief
                in (
                    InformationBeliefStatus.VERIFIED,
                    InformationBeliefStatus.REJECTED,
                )
                else bool(previous_state and previous_state.last_correction_accepted)
            ),
            ever_misbelieved=(
                bool(previous_state and previous_state.ever_misbelieved)
                or _claim_is_misbelieved(message.truth_status, belief_strength)
            ),
            ever_forgotten=bool(previous_state and previous_state.ever_forgotten),
        ).to_dict()
        if belief is InformationBeliefStatus.HEARD:
            state["query_pressure"][recipient_id] = min(
                1.0,
                state["query_pressure"][recipient_id] + config.query_pressure_per_claim,
            )
        elif accepted:
            state["query_pressure"][recipient_id] = max(
                0.0,
                state["query_pressure"][recipient_id] - config.query_pressure_relief_per_correction,
            )
            state["counters"]["correction_deliveries"] += 1
        state["counters"]["contact_deliveries"] += 1


def _information_runtime_state(world: WorldState) -> dict[str, Any]:
    try:
        config = InformationSpreadConfig.from_mapping(world.model_configs.get("information_spread"))
    except ValueError as exc:
        raise EventApplicationError(f"invalid information_spread config: {exc}") from exc
    state = world.model_state.get(INFORMATION_SPREAD_STATE_KEY)
    if not config.enabled or not isinstance(state, dict):
        raise EventApplicationError("information spread is not enabled for this run")
    try:
        return upgrade_information_runtime_state(state)
    except ValueError as exc:
        raise EventApplicationError(f"invalid information-spread state: {exc}") from exc


def _claim_is_misbelieved(truth_status: Any, belief_strength: float) -> bool:
    from sheltersim.domain.models import InformationTruthStatus

    return (truth_status is InformationTruthStatus.TRUE and belief_strength < 0.5) or (
        truth_status is InformationTruthStatus.FALSE and belief_strength > 0.5
    )


def _sample_claim_belief_strength(
    world: WorldState,
    config: InformationSpreadConfig,
    *,
    agent_id: str,
    source_id: str,
    event_id: str,
    minute: int,
    message_id: str,
) -> float:
    """Sample claimed-true belief independently from exposure and sharing."""
    reliability = config.correction_source_reliability_by_id.get(
        source_id, config.default_source_reliability
    )
    trust = world.agents[agent_id].trust
    probability = min(
        1.0,
        max(
            0.0,
            config.rumor_belief_probability
            + 2.0 * config.rumor_trust_sensitivity * (trust - 0.5)
            + 2.0 * config.rumor_source_sensitivity * (reliability - 0.5),
        ),
    )
    draw = CounterRNG(world.seed).random(
        "rumor-belief-acceptance-v1", minute, event_id, agent_id, message_id
    )
    return 1.0 if draw < probability else 0.0


def _current_information_contact_basis(
    world: WorldState,
    sender_id: str,
    recipient_id: str,
) -> str | None:
    sender = world.agents[sender_id]
    recipient = world.agents[recipient_id]
    same_zone = sender.zone_id == recipient.zone_id
    adjacent = any(
        not edge.closed
        and {
            edge.from_zone_id,
            edge.to_zone_id,
        }
        == {sender.zone_id, recipient.zone_id}
        for edge in world.zone_edges.values()
    )
    if not same_zone and not adjacent:
        return None
    if _current_household_id(world, sender.id) is not None and _current_household_id(
        world, sender.id
    ) == _current_household_id(world, recipient.id):
        return "family"
    return "same_zone" if same_zone else "adjacent_zone"


@dataclass(frozen=True)
class ResourceLedgerHandler:
    """Apply one versioned inventory operation after full validation."""

    operation: str

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        ledger_event = event
        if self.operation == "delivery" and "shipment_id" in event.payload:
            operational = world.model_state.get(OPERATIONAL_EVENTS_KEY)
            shipment_id = event.payload.get("shipment_id")
            shipment = operational.get("shipments", {}).get(shipment_id) if operational else None
            if (
                set(event.payload) != {"warehouse_id", "resource_id", "quantity", "shipment_id"}
                or not isinstance(shipment, dict)
                or shipment.get("event_id") != event.event_id
                or shipment.get("resource_id") != event.payload.get("resource_id")
                or shipment.get("due_minute") != event.due_minute
            ):
                raise EventApplicationError("shipment delivery does not match operational state")
            ledger_event = ScheduledEvent(
                event_id=event.event_id,
                kind=event.kind,
                due_minute=event.due_minute,
                priority=event.priority,
                sequence=event.sequence,
                event_type=event.event_type,
                payload={
                    key: value for key, value in event.payload.items() if key != "shipment_id"
                },
                source_id=event.source_id,
                causation_id=event.causation_id,
                provenance=dict(event.provenance),
            )
        if world.schema_version == 6 and "resource_service_v1" in world.model_state:
            service_state = world.model_state["resource_service_v1"]
            reservation_id = event.payload.get("reservation_id")
            if (
                self.operation in {"release", "consume"}
                and isinstance(reservation_id, str)
                and reservation_id in service_state["active_reservation_ids"]
            ):
                raise EventApplicationError(
                    "service-owned resource reservations must be completed or cancelled by "
                    "ResourceServiceManager"
                )
            if self.operation == "reserve" and any(
                task.id == reservation_id and task.kind == "resource_service_queue"
                for task in world.active_tasks.values()
            ):
                raise EventApplicationError(
                    "resource reservation ID conflicts with a queued resource service task"
                )
        try:
            apply_resource_operation(world, ledger_event, self.operation)
        except ResourceLedgerError as exc:
            raise EventApplicationError(str(exc)) from exc


@dataclass(frozen=True)
class FacilityOutageHandler:
    """Reduce a facility's working capacity after validating the full change."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> tuple[EventLogRecord, ...] | None:
        if set(event.payload) != {"facility_id", "units_out"}:
            raise EventApplicationError("facility outage payload fields do not match")
        facility_id = event.payload.get("facility_id")
        units_out = event.payload.get("units_out")
        if not isinstance(facility_id, str) or not facility_id:
            raise EventApplicationError("facility outage requires a facility_id")
        if not isinstance(units_out, int) or isinstance(units_out, bool):
            raise EventApplicationError("facility outage units_out must be an integer")
        if units_out < 0:
            raise EventApplicationError("facility outage units_out cannot be negative")
        facility = world.facilities.get(facility_id)
        if facility is None:
            raise EventApplicationError(f"facility {facility_id!r} does not exist")
        if active_facility_outage(world, facility_id):
            raise EventApplicationError(
                "facility outage conflicts with an active infrastructure outage"
            )
        if has_active_facility_modifier(world, facility_id):
            raise EventApplicationError(
                "facility outage conflicts with an active EffectProgram availability modifier"
            )
        if units_out == 0:
            raise EventSuppressed("zero-unit facility outage has no effect")
        previous_working_units = facility.working_units
        new_working_units = facility.working_units - units_out
        if new_working_units < 0:
            raise EventApplicationError("facility outage exceeds currently working capacity")
        # People already being served finish; no new service starts on the units out (#188).
        facility.working_units = new_working_units
        infrastructure = world.model_state.get(INFRASTRUCTURE_STATE_KEY)
        if isinstance(infrastructure, dict):
            infrastructure["base_working_units"][facility_id] = new_working_units
        recovery_record = make_facility_recovery_record(
            world,
            facility_id=facility_id,
            component_id=event.event_id,
            status="degraded",
            previous_working_units=previous_working_units,
            working_units=new_working_units,
            causation_id=event.event_id,
            source_ids=tuple(value for value in (event.source_id,) if value),
        )
        return (recovery_record[0],) if recovery_record is not None else None


@dataclass(frozen=True)
class FacilityRecoveryHandler:
    """Restore operational units without exceeding installed capacity."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> tuple[EventLogRecord, ...] | None:
        if set(event.payload) != {"facility_id", "units_restored"}:
            raise EventApplicationError("facility recovery payload fields do not match")
        facility_id = event.payload.get("facility_id")
        units_restored = event.payload.get("units_restored")
        if not isinstance(facility_id, str) or not facility_id:
            raise EventApplicationError("facility recovery requires a facility_id")
        if not isinstance(units_restored, int) or isinstance(units_restored, bool):
            raise EventApplicationError("facility recovery units_restored must be an integer")
        if units_restored < 0:
            raise EventApplicationError("facility recovery units_restored cannot be negative")
        facility = world.facilities.get(facility_id)
        if facility is None:
            raise EventApplicationError(f"facility {facility_id!r} does not exist")
        if active_facility_outage(world, facility_id):
            raise EventApplicationError(
                "facility recovery conflicts with an active infrastructure outage"
            )
        if has_active_facility_modifier(world, facility_id):
            raise EventApplicationError(
                "facility recovery conflicts with an active EffectProgram availability modifier"
            )
        if units_restored == 0:
            raise EventSuppressed("zero-unit facility recovery has no effect")
        previous_working_units = facility.working_units
        new_working_units = facility.working_units + units_restored
        if new_working_units > facility.capacity:
            raise EventApplicationError("facility recovery exceeds installed capacity")
        facility.working_units = new_working_units
        infrastructure = world.model_state.get(INFRASTRUCTURE_STATE_KEY)
        if isinstance(infrastructure, dict):
            infrastructure["base_working_units"][facility_id] = new_working_units
        recovery_record = make_facility_recovery_record(
            world,
            facility_id=facility_id,
            component_id=event.event_id,
            status="restored",
            previous_working_units=previous_working_units,
            working_units=new_working_units,
            causation_id=event.event_id,
            source_ids=tuple(value for value in (event.source_id,) if value),
        )
        return (recovery_record[0],) if recovery_record is not None else None


class InfrastructureEventHandler:
    """Apply versioned outages, time-bounded environment stress, and route disruptions."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> tuple[EventLogRecord, ...] | None:
        previous_units = {
            facility_id: facility.working_units
            for facility_id, facility in world.facilities.items()
        }
        additional_records: list[EventLogRecord] = []
        if event.event_type in {"power_outage", "equipment_failure"}:
            _apply_operational_transaction(
                world, event, lambda candidate: self._start_outage(candidate, event)
            )
        elif event.event_type in {"power_restored", "equipment_repaired"}:
            _apply_operational_transaction(
                world, event, lambda candidate: self._restore_outage(candidate, event)
            )
        elif event.event_type in {"extreme_heat", "extreme_cold"}:
            _apply_operational_transaction(
                world, event, lambda candidate: self._start_stressor(candidate, event)
            )
        elif event.event_type == "environmental_stressor_end":
            _apply_operational_transaction(
                world, event, lambda candidate: self._end_stressor(candidate, event)
            )
        elif event.event_type == "supply_route_interruption":
            _apply_operational_transaction(
                world,
                event,
                lambda candidate: additional_records.extend(
                    self._interrupt_route(candidate, event)
                ),
            )
        elif event.event_type == "supply_route_restored":
            _apply_operational_transaction(
                world, event, lambda candidate: self._restore_route(candidate, event)
            )
        else:
            raise EventApplicationError(
                f"unsupported infrastructure event type {event.event_type!r}"
            )
        component_id = next(
            (
                value
                for value in (
                    event.payload.get("component_id"),
                    event.payload.get("outage_id"),
                    event.payload.get("facility_id"),
                )
                if isinstance(value, str) and value
            ),
            event.event_id,
        )
        source_ids = tuple(
            sorted(
                {
                    value
                    for value in (
                        event.source_id,
                        event.provenance.get("source_id"),
                    )
                    if isinstance(value, str) and value
                }
            )
        )
        for facility_id, facility in sorted(world.facilities.items()):
            previous = previous_units.get(facility_id)
            current = facility.working_units
            if previous is None or current == previous:
                continue
            receipt = make_facility_recovery_record(
                world,
                facility_id=facility_id,
                component_id=component_id,
                status="degraded" if current < previous else "restored",
                previous_working_units=previous,
                working_units=current,
                causation_id=event.event_id,
                source_ids=source_ids,
            )
            if receipt is not None:
                additional_records.append(receipt[0])
        return tuple(additional_records) if additional_records else None

    def _start_outage(self, world: WorldState, event: ScheduledEvent) -> None:
        state = _infrastructure_state(world, event)
        record = _outage_for_start(state, event)
        if record["active"] or event.source_id is None:
            raise EventApplicationError("infrastructure outage is already active or lacks source")
        if event.event_type == "power_outage":
            expected = {"outage_id", "component_id", "until_minute"}
            target = event.payload.get("component_id")
            units_out = None
            completion_type = "power_restored"
        else:
            expected = {"outage_id", "facility_id", "units_out", "until_minute"}
            target = event.payload.get("facility_id")
            units_out = event.payload.get("units_out")
            completion_type = "equipment_repaired"
        until_minute = event.payload.get("until_minute")
        if (
            set(event.payload) != expected
            or not isinstance(until_minute, int)
            or isinstance(until_minute, bool)
            or until_minute <= event.due_minute
            or until_minute > world.duration_minutes
        ):
            raise EventApplicationError("infrastructure outage payload or duration is invalid")
        config = ScenarioInfrastructureSpec.model_validate(world.model_configs["infrastructure"])
        affected = dependent_facilities(
            config.model_dump(mode="json"), target, set(world.facilities)
        )
        if not affected or list(affected) != record["affected_facility_ids"]:
            raise EventApplicationError("infrastructure outage has no valid affected facilities")
        for facility_id in affected:
            if has_active_facility_modifier(world, facility_id):
                raise EventApplicationError(
                    "infrastructure outage conflicts with an active facility modifier"
                )
        record["active"] = True
        record["until_minute"] = until_minute
        record["units_out"] = units_out
        restore_id = _schedule_infrastructure_completion(
            world,
            event,
            completion_type,
            {"outage_id": event.payload["outage_id"]},
            due_minute=until_minute,
        )
        record["restore_event_id"] = restore_id
        _recalculate_infrastructure_capacity(world, event)

    def _restore_outage(self, world: WorldState, event: ScheduledEvent) -> None:
        state = _infrastructure_state(world, event)
        outage_id = event.payload.get("outage_id")
        record = state["outages"].get(outage_id)
        if (
            set(event.payload) != {"outage_id"}
            or record is None
            or not record["active"]
            or record["restore_event_id"] != event.event_id
            or record["start_event_id"] != event.causation_id
            or event.source_id is None
        ):
            raise EventApplicationError(
                "infrastructure restoration does not match an active outage"
            )
        expected_type = (
            "power_restored" if record["event_type"] == "power_outage" else "equipment_repaired"
        )
        if event.event_type != expected_type:
            raise EventApplicationError("infrastructure restoration type does not match its cause")
        record["active"] = False
        _recalculate_infrastructure_capacity(world, event)

    def _start_stressor(self, world: WorldState, event: ScheduledEvent) -> None:
        state = _infrastructure_state(world, event)
        stressor_id = event.payload.get("stressor_id")
        record = state["environmental_stressors"].get(stressor_id)
        until_minute = event.payload.get("until_minute")
        if (
            set(event.payload) != {"stressor_id", "zone_id", "until_minute"}
            or record is None
            or record["start_event_id"] != event.event_id
            or record["active"]
            or event.payload.get("zone_id") not in world.zones
            or event.source_id is None
            or not isinstance(until_minute, int)
            or isinstance(until_minute, bool)
            or until_minute <= event.due_minute
            or until_minute > world.duration_minutes
        ):
            raise EventApplicationError("environmental stressor payload or duration is invalid")
        active_count = sum(
            other["active"] and other["zone_id"] == record["zone_id"]
            for other in state["environmental_stressors"].values()
        )
        if active_count >= 16:
            raise EventApplicationError("zone environmental stressor limit was reached")
        record["active"] = True
        end_id = _schedule_infrastructure_completion(
            world,
            event,
            "environmental_stressor_end",
            {"stressor_id": stressor_id, "zone_id": record["zone_id"]},
            due_minute=until_minute,
        )
        record["end_event_id"] = end_id
        record["until_minute"] = until_minute

    def _end_stressor(self, world: WorldState, event: ScheduledEvent) -> None:
        state = _infrastructure_state(world, event)
        stressor_id = event.payload.get("stressor_id")
        record = state["environmental_stressors"].get(stressor_id)
        if (
            set(event.payload) != {"stressor_id", "zone_id"}
            or record is None
            or not record["active"]
            or record["end_event_id"] != event.event_id
            or record["start_event_id"] != event.causation_id
            or record["zone_id"] != event.payload.get("zone_id")
            or event.source_id is None
        ):
            raise EventApplicationError("environmental end event does not match an active stressor")
        record["active"] = False

    def _interrupt_route(
        self, world: WorldState, event: ScheduledEvent
    ) -> tuple[EventLogRecord, ...]:
        state = _infrastructure_state(world, event)
        outage_id = event.payload.get("outage_id")
        record = state["route_interruptions"].get(outage_id)
        until_minute = event.payload.get("until_minute")
        if (
            set(event.payload) != {"outage_id", "route_id", "shipment_ids", "until_minute"}
            or record is None
            or record["start_event_id"] != event.event_id
            or record["active"]
            or not isinstance(until_minute, int)
            or isinstance(until_minute, bool)
            or until_minute <= event.due_minute
            or until_minute > world.duration_minutes
            or event.source_id is None
        ):
            raise EventApplicationError("supply route interruption payload or duration is invalid")
        if any(
            other["active"] and other["route_id"] == record["route_id"]
            for other in state["route_interruptions"].values()
        ):
            raise EventApplicationError("supply route already has an active interruption")
        operational = world.model_state.get(OPERATIONAL_EVENTS_KEY)
        if operational is None:
            raise EventApplicationError("supply route interruption requires shipment records")
        delay = until_minute - event.due_minute
        delivery_delays: list[tuple[str, int]] = []
        for shipment_id in record["shipment_ids"]:
            shipment = operational["shipments"].get(shipment_id)
            delivery = next(
                (
                    pending
                    for pending in world.event_queue.pending()
                    if shipment is not None and pending.event_id == shipment["event_id"]
                ),
                None,
            )
            if (
                delivery is None
                or delivery.event_type != "resource_delivery"
                or delivery.due_minute <= event.due_minute
                or delivery.due_minute != shipment["due_minute"]
                or delivery.due_minute + delay >= world.duration_minutes
            ):
                raise EventApplicationError(
                    f"shipment {shipment_id!r} cannot be safely delayed by this route event"
                )
            delivery_delays.append((shipment_id, delivery.due_minute + delay))
        delayed_records: list[EventLogRecord] = []
        for shipment_id, new_due_minute in delivery_delays:
            sequence = world.allocate_event_sequence()
            delayed = ScheduledEvent(
                event_id=f"route-delay-{sequence}",
                kind=EventKind.COMPLETION,
                due_minute=event.due_minute,
                priority=-10,
                sequence=sequence,
                event_type="delivery_delayed",
                payload={"shipment_id": shipment_id, "to_minute": new_due_minute},
                source_id=event.source_id,
                causation_id=event.event_id,
                provenance={
                    "source_type": "assumption",
                    "source_id": ScenarioInfrastructureSpec.model_validate(
                        world.model_configs["infrastructure"]
                    ).source_id,
                    "policy_version": "infrastructure-events-v1",
                    "route_id": record["route_id"],
                },
            )
            _ensure_event_identity_free(world, delayed)
            world.schedule_event(delayed)
            DeliveryDelayHandler().apply(world, delayed)
            world.event_queue.remove(delayed.event_id)
            delayed_records.append(
                EventLogRecord(
                    event=delayed,
                    realized_minute=world.minute_index,
                    status=EventStatus.APPLIED,
                )
            )
            record = world.model_state[INFRASTRUCTURE_STATE_KEY]["route_interruptions"][outage_id]
        record["active"] = True
        record["until_minute"] = until_minute
        restore_id = _schedule_infrastructure_completion(
            world,
            event,
            "supply_route_restored",
            {"outage_id": outage_id, "route_id": record["route_id"]},
            due_minute=until_minute,
        )
        record["restore_event_id"] = restore_id
        return tuple(delayed_records)

    def _restore_route(self, world: WorldState, event: ScheduledEvent) -> None:
        state = _infrastructure_state(world, event)
        outage_id = event.payload.get("outage_id")
        record = state["route_interruptions"].get(outage_id)
        if (
            set(event.payload) != {"outage_id", "route_id"}
            or record is None
            or not record["active"]
            or record["restore_event_id"] != event.event_id
            or record["start_event_id"] != event.causation_id
            or record["route_id"] != event.payload.get("route_id")
            or event.source_id is None
        ):
            raise EventApplicationError("route restoration does not match an active interruption")
        record["active"] = False


def _infrastructure_state(world: WorldState, event: ScheduledEvent) -> dict[str, Any]:
    state = world.model_state.get(INFRASTRUCTURE_STATE_KEY)
    if not isinstance(state, dict):
        raise EventApplicationError(
            f"{event.event_type} requires the opted-in infrastructure model"
        )
    return state


def _outage_for_start(state: dict[str, Any], event: ScheduledEvent) -> dict[str, Any]:
    outage_id = event.payload.get("outage_id")
    record = state["outages"].get(outage_id)
    if record is None or record["start_event_id"] != event.event_id:
        raise EventApplicationError("infrastructure outage does not match scenario state")
    if record["event_type"] != event.event_type:
        raise EventApplicationError(
            "infrastructure outage event type does not match scenario state"
        )
    return record


def _schedule_infrastructure_completion(
    world: WorldState,
    cause: ScheduledEvent,
    event_type: str,
    payload: dict[str, Any],
    *,
    due_minute: int,
) -> str | None:
    if due_minute >= world.duration_minutes:
        return None
    sequence = world.allocate_event_sequence()
    event = ScheduledEvent(
        event_id=f"{event_type.replace('_', '-')}-{sequence}",
        kind=EventKind.COMPLETION,
        due_minute=due_minute,
        priority=10,
        sequence=sequence,
        event_type=event_type,
        payload=payload,
        source_id=cause.source_id,
        causation_id=cause.event_id,
        provenance={
            "source_type": "assumption",
            "source_id": ScenarioInfrastructureSpec.model_validate(
                world.model_configs["infrastructure"]
            ).source_id,
            "policy_version": "infrastructure-events-v1",
        },
    )
    _ensure_event_identity_free(world, event)
    world.schedule_event(event)
    return event.event_id


def _recalculate_infrastructure_capacity(world: WorldState, cause: ScheduledEvent) -> None:
    state = _infrastructure_state(world, cause)
    outage_id = cause.payload.get("outage_id")
    changed_outage = state["outages"].get(outage_id)
    if changed_outage is None:
        raise EventApplicationError("infrastructure capacity update references an unknown outage")
    changed_facility_ids = set(changed_outage["affected_facility_ids"])
    try:
        capacities = effective_facility_capacities(
            state["base_working_units"],
            state["outages"],
            world.model_configs["infrastructure"],
            set(world.facilities),
        )
    except ValueError as exc:
        raise EventApplicationError(str(exc)) from exc
    for facility_id in sorted(changed_facility_ids):
        # As with a facility outage, current users finish (#188).
        world.facilities[facility_id].working_units = capacities[facility_id]


@dataclass(frozen=True)
class SocialInteractionHandler:
    """Apply one allowlisted, bounded social proposal at its scheduled minute."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        interaction_type = event.payload.get("interaction_type")
        if interaction_type is not None:
            self._apply_typed_interaction(world, event, interaction_type)
            return
        agent_id = event.payload.get("agent_id")
        request_id = event.payload.get("request_id")
        action = event.payload.get("action")
        if not all(isinstance(item, str) and item for item in (agent_id, request_id, action)):
            raise EventApplicationError(
                "social interaction requires Agent, request, and action IDs"
            )
        request = world.support_requests.get(request_id)
        agent = world.agents.get(agent_id)
        if request is None or request.agent_id != agent_id or agent is None:
            raise EventApplicationError("social interaction references an unknown request or Agent")
        if action == "offer_reassurance":
            agent.stress = max(0.0, agent.stress - 0.03)
            agent.trust = min(1.0, agent.trust + 0.01)
        elif action == "share_practical_information":
            agent.trust = min(1.0, agent.trust + 0.005)
        else:
            raise EventApplicationError("social interaction action is not allowed")

    def _apply_typed_interaction(
        self, world: WorldState, event: ScheduledEvent, interaction_type: Any
    ) -> None:
        if (
            event.kind is not EventKind.ENDOGENOUS
            or event.due_minute != world.minute_index
            or event.provenance.get("source_id") != "ASM-EVENT-TRIGGERED-SOCIAL-INTERACTIONS-V1"
            or event.provenance.get("source_type") != "assumption"
            or event.source_id != event.provenance.get("policy_version")
        ):
            raise EventApplicationError("typed social interaction provenance or time is invalid")
        if event.payload.get("reason_short") is None:
            raise EventApplicationError("typed social interaction requires a reason code")
        agent_id = event.payload.get("agent_id")
        agent = world.agents.get(agent_id) if isinstance(agent_id, str) else None
        if agent is None:
            raise EventApplicationError("typed social interaction references an unknown Agent")
        if "conversation_state_context" in event.payload:
            participant_id = event.payload.get("participant_agent_id")
            participant_ids = [agent_id]
            if isinstance(participant_id, str):
                participant_ids.append(participant_id)
            try:
                validate_social_state_context(
                    event.payload["conversation_state_context"],
                    participant_ids,
                    event.due_minute,
                )
            except (TypeError, ValueError) as exc:
                raise EventApplicationError("social dialogue state context is invalid") from exc
        action = event.payload.get("action")

        if interaction_type in {"reassurance_exchange", "family_care", "voluntary_support"}:
            expected = {
                "interaction_type",
                "agent_id",
                "participant_agent_id",
                "zone_id",
                "topic_id",
                "action",
                "reason_short",
                "interaction_duration_minutes",
            }
            if _social_payload_fields(event.payload) != expected:
                raise EventApplicationError("autonomous social action payload fields do not match")
            participant_id = event.payload.get("participant_agent_id")
            participant = (
                world.agents.get(participant_id) if isinstance(participant_id, str) else None
            )
            if (
                participant is None
                or participant.id == agent.id
                or event.payload.get("zone_id") != agent.zone_id
                or participant.zone_id != agent.zone_id
            ):
                raise EventSuppressed("social action participants are no longer co-located")
            action_by_type = {
                "reassurance_exchange": ("offer_reassurance", "emotional_check_in"),
                "family_care": ("provide_family_support", "family_care"),
                "voluntary_support": ("offer_practical_help", "mutual_aid"),
            }
            expected_action, expected_topic = action_by_type[interaction_type]
            if action != expected_action or event.payload.get("topic_id") != expected_topic:
                raise EventApplicationError("social action does not match its typed topic")
            if event.payload.get("reason_short") != "autonomous_need_choice":
                raise EventApplicationError("social action reason code is not allowed")
            duration = event.payload.get("interaction_duration_minutes")
            if type(duration) is not int or not 1 <= duration <= 1440:
                raise EventApplicationError("social action duration must be within 1..1440 minutes")
            if interaction_type == "family_care" and (
                _current_household_id(world, agent.id) is None
                or _current_household_id(world, agent.id)
                != _current_household_id(world, participant.id)
            ):
                raise EventSuppressed("family support target is no longer in the same household")
            if interaction_type == "voluntary_support":
                actor_need = max(
                    agent.stress,
                    agent.needs.toilet_urgency,
                    agent.needs.hunger,
                    agent.needs.fatigue,
                    agent.needs.hydration_need,
                )
                target_need = max(
                    participant.stress,
                    participant.needs.toilet_urgency,
                    participant.needs.hunger,
                    participant.needs.fatigue,
                    participant.needs.hydration_need,
                )
                if target_need <= actor_need + 0.15:
                    raise EventSuppressed(
                        "the selected person no longer has a higher immediate need"
                    )
            body = world.model_state.get("body_state_v1", {}).get("agents", {})
            actor_body = body.get(agent.id, {})
            positive_signal = actor_body.get("positive_affect", 0.5) - actor_body.get(
                "negative_affect", 0.0
            )
            signal = min(1.0, max(-1.0, positive_signal))
            state = world.model_state.get("body_state_v1", {})
            config = state.get("config", {}) if isinstance(state, dict) else {}
            transfer_rate = config.get("social_action_affect_transfer", 0.02)
            relief = transfer_rate * signal
            participant.stress = min(1.0, max(0.0, participant.stress - relief))
            participant.trust = min(1.0, max(0.0, participant.trust + relief * 0.5))
            if interaction_type != "reassurance_exchange":
                agent.needs.fatigue = min(1.0, agent.needs.fatigue + 0.002)
                agent.trust = min(1.0, agent.trust + max(0.0, relief * 0.25))
            return

        if interaction_type == "complaint_response":
            expected = {
                "interaction_type",
                "agent_id",
                "request_id",
                "zone_id",
                "stress_threshold",
                "complaint_wait_minutes",
                "action",
                "reason_short",
            }
            if _social_payload_fields(event.payload) != expected:
                raise EventApplicationError("complaint response payload fields do not match")
            request_id = event.payload.get("request_id")
            request = world.support_requests.get(request_id)
            stress_threshold = event.payload.get("stress_threshold")
            complaint_wait = event.payload.get("complaint_wait_minutes")
            if (
                not isinstance(stress_threshold, (int, float))
                or isinstance(stress_threshold, bool)
                or not 0.0 <= stress_threshold <= 1.0
                or not isinstance(complaint_wait, int)
                or isinstance(complaint_wait, bool)
                or complaint_wait <= 0
            ):
                raise EventApplicationError("complaint response thresholds are invalid")
            if (
                request is None
                or request.agent_id != agent_id
                or request.status is not SupportRequestStatus.PENDING
                or event.payload.get("zone_id") != agent.zone_id
                or event.causation_id != request_id
                or event.due_minute - request.requested_minute < complaint_wait
            ):
                raise EventSuppressed("complaint response context is no longer current")
            if agent.stress < stress_threshold:
                raise EventSuppressed("complaint response stress candidate is no longer current")
            if event.payload.get("reason_short") not in {
                "long_wait",
                "high_stress",
                "request_still_pending",
                "rule_fallback",
            }:
                raise EventApplicationError("complaint response reason code is not allowed")
            if action == "acknowledge_complaint":
                agent.stress = max(0.0, agent.stress - 0.02)
                agent.trust = min(1.0, agent.trust + 0.01)
            elif action == "explain_staff_process":
                agent.stress = max(0.0, agent.stress - 0.01)
                agent.trust = min(1.0, agent.trust + 0.01)
            elif action == "share_practical_information":
                agent.trust = min(1.0, agent.trust + 0.005)
            else:
                raise EventApplicationError("complaint response action is not allowed")
            return

        if interaction_type == "cooperation":
            expected = {
                "interaction_type",
                "agent_id",
                "participant_agent_id",
                "zone_id",
                "stress_threshold",
                "relationship",
                "trigger_event_id",
                "action",
                "reason_short",
            }
            if _social_payload_fields(event.payload) != expected:
                raise EventApplicationError("cooperation payload fields do not match")
            participant_id = event.payload.get("participant_agent_id")
            participant = (
                world.agents.get(participant_id) if isinstance(participant_id, str) else None
            )
            trigger_event_id = event.payload.get("trigger_event_id")
            stress_threshold = event.payload.get("stress_threshold")
            if (
                not isinstance(stress_threshold, (int, float))
                or isinstance(stress_threshold, bool)
                or not 0.0 <= stress_threshold <= 1.0
            ):
                raise EventApplicationError("cooperation stress threshold is invalid")
            if participant is None or participant.id == agent.id:
                raise EventApplicationError("cooperation requires two different known Agents")
            same_group = _current_household_id(
                world, agent.id
            ) is not None and _current_household_id(world, agent.id) == _current_household_id(
                world, participant.id
            )
            if (
                event.payload.get("zone_id") != agent.zone_id
                or agent.zone_id != participant.zone_id
                or not same_group
                or event.payload.get("relationship") != "same_group"
                or event.causation_id != trigger_event_id
            ):
                raise EventSuppressed("cooperation participants or relationship changed")
            contact = (
                world.event_log.find(trigger_event_id)
                if isinstance(trigger_event_id, str)
                else None
            )
            if (
                contact is None
                or contact.status is not EventStatus.APPLIED
                or contact.event.event_type != "information_contact_update"
                or contact.realized_minute != event.due_minute - 1
                or contact.event.payload.get("sender_agent_id") != agent_id
                or contact.event.payload.get("recipient_agent_id") != participant.id
            ):
                raise EventApplicationError("cooperation does not match an applied local contact")
            if max(agent.stress, participant.stress) < stress_threshold:
                raise EventSuppressed("cooperation stress candidate is no longer current")
            if event.payload.get("reason_short") not in {
                "shared_group_contact",
                "elevated_stress",
                "rule_fallback",
            }:
                raise EventApplicationError("cooperation reason code is not allowed")
            if action != "offer_mutual_support":
                raise EventApplicationError("cooperation action is not allowed")
            agent.stress = max(0.0, agent.stress - 0.02)
            participant.stress = max(0.0, participant.stress - 0.02)
            agent.trust = min(1.0, agent.trust + 0.01)
            participant.trust = min(1.0, participant.trust + 0.01)
            return

        if interaction_type == "information_reaction":
            expected = {
                "interaction_type",
                "agent_id",
                "participant_agent_id",
                "zone_id",
                "message_id",
                "trigger_event_id",
                "action",
                "reason_short",
            }
            if _social_payload_fields(event.payload) != expected:
                raise EventApplicationError("information reaction payload fields do not match")
            participant_id = event.payload.get("participant_agent_id")
            participant = (
                world.agents.get(participant_id) if isinstance(participant_id, str) else None
            )
            message_id = event.payload.get("message_id")
            trigger_event_id = event.payload.get("trigger_event_id")
            if participant is None or participant.id == agent.id:
                raise EventApplicationError("information reaction requires two known Agents")
            if (
                event.payload.get("zone_id") != agent.zone_id
                or agent.zone_id != participant.zone_id
                or event.causation_id != trigger_event_id
            ):
                raise EventSuppressed("information reaction contact is no longer local")
            contact = (
                world.event_log.find(trigger_event_id)
                if isinstance(trigger_event_id, str)
                else None
            )
            if (
                contact is None
                or contact.status is not EventStatus.APPLIED
                or contact.event.event_type != "information_contact_update"
                or contact.realized_minute != event.due_minute - 1
                or contact.event.payload.get("recipient_agent_id") != agent_id
                or contact.event.payload.get("sender_agent_id") != participant_id
            ):
                raise EventApplicationError(
                    "information reaction does not match an applied contact"
                )
            state = world.model_state.get(INFORMATION_SPREAD_STATE_KEY)
            if not isinstance(state, dict):
                raise EventSuppressed("information state is no longer enabled")
            raw_message = state["messages"].get(message_id)
            raw_belief = state["beliefs"].get(agent_id, {}).get(message_id)
            if raw_message is None or raw_belief is None:
                raise EventSuppressed("information claim is no longer current")
            message = TypedMessage.from_dict(raw_message)
            belief = AgentInformationState.from_dict(raw_belief)
            if (
                message.truth_status is not InformationTruthStatus.FALSE
                or belief.belief is not InformationBeliefStatus.HEARD
            ):
                raise EventSuppressed("information claim has already been verified or rejected")
            if action != "seek_claim_verification":
                raise EventApplicationError("information reaction action is not allowed")
            if event.payload.get("reason_short") not in {
                "heard_through_contact",
                "request_verification",
                "rule_fallback",
            }:
                raise EventApplicationError("information reaction reason code is not allowed")
            config = InformationSpreadConfig.from_mapping(world.model_configs["information_spread"])
            state["query_pressure"][agent_id] = min(
                1.0,
                state["query_pressure"][agent_id] + config.query_pressure_per_claim,
            )
            return

        raise EventApplicationError("typed social interaction type is not allowed")


@dataclass(frozen=True)
class EffectProgramPopulationHandler:
    """Apply typed population primitives through the approved EffectProgram record."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        from sheltersim.core.world import WorldState as MutableWorldState
        from sheltersim.domain.population import (
            apply_arrival_approach_completion,
            queue_arrivals,
            queue_departures,
        )
        from sheltersim.domain.population_lifecycle import (
            apply_population_presence_command,
        )
        from sheltersim.domain.simulator_state import validate_simulator_extensions

        if event.event_type == "population_approach_complete":
            apply_arrival_approach_completion(world, event)
            return
        op = event.payload.get("op")
        expected_base = {"program_id", "effect_id", "op", "zone_id", "count"}
        if op == "add_evacuees":
            expected = expected_base | {
                "household_size",
                "mobility",
                "language",
                "admission_zone_ids",
                "approach_route_id",
                "person_context_overrides",
            }
            previous_expected = expected - {"person_context_overrides"}
            prior_arrival_expected = previous_expected - {"approach_route_id"}
            legacy_expected = expected_base | {"household_size", "mobility", "language"}
        elif op == "depart_evacuees":
            expected = expected_base | {"include_initial"}
            legacy_expected = expected_base
        elif op == "population_presence":
            expected = {"program_id", "effect_id", "op", "command"}
        else:
            raise EventApplicationError("population primitive operation is invalid")
        if set(event.payload) != expected and not (
            op == "add_evacuees"
            and frozenset(event.payload)
            in {
                frozenset(previous_expected),
                frozenset(prior_arrival_expected),
                frozenset(legacy_expected),
            }
            or op == "depart_evacuees"
            and set(event.payload) == legacy_expected
        ):
            raise EventApplicationError("population primitive payload fields do not match")
        state = world.model_state.get("effect_program_v1")
        program = (
            state.get("programs", {}).get(event.payload.get("program_id"))
            if isinstance(state, dict)
            else None
        )
        effect_id = event.payload.get("effect_id")
        if (
            not isinstance(program, dict)
            or effect_id not in program.get("effect_ids", [])
            or program.get("operators", {}).get(effect_id) != op
            or event.event_id not in program.get("scheduled_event_ids", [])
            or (op != "population_presence" and event.payload.get("zone_id") not in world.zones)
        ):
            raise EventApplicationError("population primitive is not in the approved program")
        if op == "population_presence":
            apply_population_presence_command(
                world,
                event,
                event.payload["command"],
            )
            return
        candidate = MutableWorldState.from_dict(world.to_dict())
        try:
            if op == "add_evacuees":
                queue_arrivals(
                    candidate,
                    event=event,
                    zone_id=event.payload["zone_id"],
                    count=event.payload["count"],
                    household_size=event.payload["household_size"],
                    mobility=event.payload["mobility"],
                    language=event.payload["language"],
                    admission_zone_ids=tuple(event.payload.get("admission_zone_ids", ())),
                    approach_route_id=event.payload.get("approach_route_id"),
                    person_context_overrides=tuple(
                        event.payload.get("person_context_overrides", ())
                    ),
                )
            else:
                queue_departures(
                    candidate,
                    event=event,
                    zone_id=event.payload["zone_id"],
                    count=event.payload["count"],
                    include_initial=event.payload.get("include_initial", False),
                )
                from sheltersim.domain.population_lifecycle import admit_pending_returns

                admit_pending_returns(candidate, event.due_minute)
        except EventSuppressed:
            candidate.validate()
            validate_simulator_extensions(candidate)
            world.__dict__.clear()
            world.__dict__.update(candidate.__dict__)
            raise
        except EventApplicationError:
            raise
        except (TypeError, ValueError) as exc:
            raise EventApplicationError(str(exc)) from exc
        candidate.validate()
        validate_simulator_extensions(candidate)
        world.__dict__.clear()
        world.__dict__.update(candidate.__dict__)


@dataclass(frozen=True)
class ToiletServiceCompletionHandler:
    """Apply one due service completion, releasing its reserved toilet slot."""

    after_complete: Callable[[WorldState, ScheduledEvent], None] | None = None

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        facility_id = event.payload.get("facility_id")
        agent_id = event.payload.get("agent_id")
        task_id = event.payload.get("task_id")
        if not all(isinstance(item, str) and item for item in (facility_id, agent_id, task_id)):
            raise EventApplicationError(
                "toilet completion requires facility_id, agent_id, and task_id"
            )
        task = world.active_tasks.get(task_id)
        if task is None or task.due_minute != event.due_minute:
            raise EventApplicationError("toilet completion does not match an active due task")
        try:
            complete_toilet_service(
                world,
                facility_id=facility_id,
                agent_id=agent_id,
                task_id=task_id,
            )
        except ValueError as exc:
            raise EventApplicationError(str(exc)) from exc
        body = world.model_state.get("body_state_v1", {}).get("agents", {})
        if agent_id in body:
            # A completed sanitation visit can reduce hygiene pressure; it does
            # not erase the need when the facility itself is poorly maintained.
            body[agent_id]["hygiene_need"] = max(0.0, body[agent_id]["hygiene_need"] - 0.2)
        if self.after_complete is not None:
            self.after_complete(world, event)


@dataclass(frozen=True)
class AgentTravelCompletionHandler:
    """Complete a scheduled walk and optionally register its toilet request."""

    toilet_service: ToiletServiceManager
    resource_service: ResourceServiceManager | None = None
    validate_request_state: bool = True

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        agent_id = event.payload.get("agent_id")
        task_id = event.payload.get("task_id")
        destination_zone_id = event.payload.get("destination_zone_id")
        purpose = event.payload.get("purpose")
        facility_id = event.payload.get("facility_id")
        origin_zone_id = event.payload.get("origin_zone_id")
        if not isinstance(origin_zone_id, str):
            route_zones = event.payload.get("route_zone_ids", ())
            origin_zone_id = (
                route_zones[0] if isinstance(route_zones, (list, tuple)) and route_zones else None
            )
        if not all(
            isinstance(item, str) and item
            for item in (agent_id, task_id, destination_zone_id, purpose)
        ):
            raise EventApplicationError(
                "travel completion requires Agent, task, destination, and purpose"
            )
        task = world.active_tasks.get(task_id)
        agent = world.agents.get(agent_id)
        queue_purpose = purpose in {
            "toilet_queue_position",
            "resource_queue_position",
            "toilet_queue_service",
            "resource_queue_service",
        }
        if task is None or task.kind not in (
            "travel_to_toilet",
            "travel_to_resource",
            "travel_return",
            "staff_support_travel",
            "toilet_queue",
            "resource_service_queue",
        ):
            raise EventApplicationError("travel completion does not match an active task")
        if agent is None or task.agent_id != agent_id:
            raise EventApplicationError("travel completion Agent does not match its active task")
        if queue_purpose:
            resource = purpose.startswith("resource_")
            expected_kind = "resource_service_queue" if resource else "toilet_queue"
            facility = world.facilities.get(facility_id) if isinstance(facility_id, str) else None
            queue = (
                facility.resource_queue_agent_ids
                if resource and facility is not None
                else facility.queue_agent_ids
                if facility is not None
                else ()
            )
            movement = world.model_state.get("movement_v1", {})
            journey = (
                movement.get("journeys", {}).get(agent_id, {}) if isinstance(movement, dict) else {}
            )
            from sheltersim.domain.facility_queues import (
                FACILITY_QUEUES_STATE_KEY,
                facility_has_independent_queues,
            )

            independent = isinstance(facility_id, str) and facility_has_independent_queues(
                world, facility_id
            )
            request_id = event.payload.get("request_id")
            queue_id = event.payload.get("queue_id")
            if independent:
                queue_state = world.model_state[FACILITY_QUEUES_STATE_KEY]
                request = (
                    queue_state["requests"].get(request_id) if isinstance(request_id, str) else None
                )
                queue_row = (
                    queue_state["queues"].get(queue_id) if isinstance(queue_id, str) else None
                )
                queue_contains_request = (
                    isinstance(request, dict)
                    and isinstance(queue_row, dict)
                    and request_id in queue_row["queued_request_ids"]
                    and request["agent_id"] == agent_id
                    and request["facility_id"] == facility_id
                    and request["queue_id"] == queue_id
                )
                service_point_id = (
                    request.get("service_point_id") if isinstance(request, dict) else None
                )
                at_reserved_destination = (
                    agent_is_at_queue_slot(world, agent_id, facility_id, queue_id)
                    or agent_is_at_queue_trip_destination(world, agent_id, journey)
                    if purpose.endswith("_position") and isinstance(facility_id, str)
                    else agent_is_at_facility_service_point(
                        world, agent_id, facility_id, service_point_id
                    )
                    if isinstance(facility_id, str) and isinstance(service_point_id, str)
                    else False
                )
            else:
                queue_contains_request = agent_id in queue
                at_reserved_destination = (
                    agent_is_at_queue_slot(world, agent_id, facility_id)
                    or agent_is_at_queue_trip_destination(world, agent_id, journey)
                    if purpose.endswith("_position") and isinstance(facility_id, str)
                    else agent_is_at_facility_service_point(world, agent_id, facility_id)
                    if isinstance(facility_id, str)
                    else False
                )
            if (
                task.kind != expected_kind
                or task.due_minute != task.start_minute
                or agent.activity is not Activity.QUEUED
                or agent.target_id != facility_id
                or not queue_contains_request
                or facility is None
                or facility.zone_id != destination_zone_id
                or agent.zone_id != destination_zone_id
                or not at_reserved_destination
                or (
                    journey
                    and (
                        journey.get("event_id") != event.event_id
                        or journey.get("task_id") != task_id
                        or journey.get("purpose") != purpose
                        or journey.get("due_minute") != event.due_minute
                        or (independent and journey.get("queue_id") != queue_id)
                    )
                )
                or (not journey and event.due_minute != world.minute_index)
            ):
                raise EventApplicationError(
                    "physical queue travel does not match its FIFO reservation"
                )
            return
        if task.due_minute != event.due_minute:
            raise EventApplicationError("travel completion does not match an active due task")
        if agent.activity is not Activity.MOVING:
            raise EventApplicationError("travel completion Agent does not match its active task")
        if destination_zone_id not in world.zones:
            raise EventApplicationError(f"destination zone {destination_zone_id!r} does not exist")
        if purpose == "toilet_request":
            if not isinstance(facility_id, str) or not facility_id:
                raise EventApplicationError("toilet travel completion requires a facility_id")
            facility = world.facilities.get(facility_id)
            if facility is None or facility.zone_id != destination_zone_id:
                raise EventApplicationError("toilet travel destination does not match its facility")
            if task.kind != "travel_to_toilet" or agent.target_id != facility_id:
                raise EventApplicationError("toilet travel completion does not match its target")
        elif purpose == "resource_request":
            if self.resource_service is None:
                raise EventApplicationError("resource travel is not enabled for this scenario")
            if not isinstance(facility_id, str) or not facility_id:
                raise EventApplicationError("resource travel completion requires a facility_id")
            route_zone_ids = event.payload.get("route_zone_ids")
            facility = world.facilities.get(facility_id)
            if (
                facility is None
                or facility.zone_id != destination_zone_id
                or not isinstance(origin_zone_id, str)
                or origin_zone_id not in world.zones
                or not isinstance(route_zone_ids, (list, tuple))
                or not route_zone_ids
                or route_zone_ids[0] != origin_zone_id
                or route_zone_ids[-1] != destination_zone_id
                or ResourceServiceManager.resource_for_facility(facility.kind) is None
                or task.kind != "travel_to_resource"
                or agent.target_id != facility_id
            ):
                raise EventApplicationError(
                    "resource travel completion does not match its target facility"
                )
        elif purpose in {"return", "queue_abandon"}:
            if task.kind != "travel_return" or agent.target_id != destination_zone_id:
                raise EventApplicationError("return travel completion does not match its target")
        elif purpose == "staff_support_travel":
            request_id = event.payload.get("request_id")
            staff_id = event.payload.get("staff_id")
            request = (
                world.support_requests.get(request_id) if isinstance(request_id, str) else None
            )
            staff_member = world.staff.get(staff_id) if isinstance(staff_id, str) else None
            if (
                task.kind != "staff_support_travel"
                or request is None
                or request.agent_id != agent_id
                or request.status is not SupportRequestStatus.ASSIGNED
                or request.assigned_staff_id != staff_id
                or request.task_id is None
                or agent.support_request_id != request.id
                or staff_member is None
                or staff_member.zone_id != destination_zone_id
                or agent.target_id != destination_zone_id
                or task.due_minute != event.due_minute
            ):
                raise EventApplicationError(
                    "staff support travel does not match its assigned request and staff member"
                )
        elif purpose == "sleep_bed":
            overlay = world.model_state.get("geometry_overlay_v1", {})
            reservations = overlay.get("bed_reservations", {}) if isinstance(overlay, dict) else {}
            reservation = reservations.get(agent_id) if isinstance(reservations, dict) else None
            rooms = overlay.get("temporary_rooms", {}) if isinstance(overlay, dict) else {}
            room = rooms.get(reservation.get("room_id")) if isinstance(reservation, dict) else None
            movement = world.model_state.get(MOVEMENT_STATE_KEY, {})
            journey = (
                movement.get("journeys", {}).get(agent_id) if isinstance(movement, dict) else None
            )
            bed_index = reservation.get("bed_index") if isinstance(reservation, dict) else None
            bed = (
                room.get("bed_points_m", [])[bed_index]
                if isinstance(room, dict)
                and type(bed_index) is int
                and 0 <= bed_index < len(room.get("bed_points_m", []))
                else None
            )
            route_points = journey.get("route_points", []) if isinstance(journey, dict) else []
            if (
                task.kind != "travel_return"
                or agent.target_id != destination_zone_id
                or not isinstance(reservation, dict)
                or not isinstance(room, dict)
                or room.get("zone_id") != destination_zone_id
                or not isinstance(journey, dict)
                or journey.get("event_id") != event.event_id
                or journey.get("task_id") != task_id
                or journey.get("purpose") != "sleep_bed"
                or not route_points
                or route_points[-1] != bed
            ):
                raise EventApplicationError("sleep-bed travel does not match its live reservation")
        elif purpose == "presence_relocation":
            from sheltersim.domain.population_lifecycle import POPULATION_LIFECYCLE_STATE_KEY

            lifecycle = world.model_state.get(POPULATION_LIFECYCLE_STATE_KEY, {})
            relocation = lifecycle.get("pending_relocations", {}).get(agent_id)
            if (
                task.kind != "travel_return"
                or agent.target_id != destination_zone_id
                or not isinstance(relocation, dict)
                or relocation.get("task_id") != task_id
                or relocation.get("event_id") != event.event_id
                or relocation.get("from_zone_id") != origin_zone_id
                or relocation.get("to_zone_id") != destination_zone_id
            ):
                raise EventApplicationError(
                    "presence relocation completion does not match its route"
                )
        else:
            raise EventApplicationError(f"unsupported travel purpose {purpose!r}")

        if purpose == "sleep_bed":
            movement["positions"][agent_id].update(
                {
                    "zone_id": destination_zone_id,
                    "point": bed,
                    "edge_id": None,
                    "progress": 1.0,
                    "position_kind": "physical",
                }
            )
        del world.active_tasks[task_id]
        agent.zone_id = destination_zone_id
        # A person who walked to a temporary room's bed chose to sleep, so they
        # sleep on arrival instead of idling until their next decision (#84).
        agent.activity = (
            Activity.SEEKING_HELP
            if purpose == "staff_support_travel"
            else Activity.SLEEPING
            if purpose == "sleep_bed"
            else Activity.IDLE
        )
        agent.target_id = None
        if purpose == "presence_relocation":
            from sheltersim.domain.population_lifecycle import complete_presence_relocation

            complete_presence_relocation(world, event)
        if (
            purpose in {"toilet_request", "resource_request"}
            and "facility_geometry" in world.model_configs
            and isinstance(facility_id, str)
        ):
            from sheltersim.domain.facility_queues import facility_has_independent_queues

            if not facility_has_independent_queues(world, facility_id):
                approach = facility_approach_route(world, agent_id, facility_id)
                if approach is None:
                    self._reject_room_approach(world, event, agent_id, facility_id, origin_zone_id)
                    return
                local_route, route_points = approach
                if local_route.travel_minutes > 0:
                    if self._schedule_room_approach(
                        world,
                        agent_id=agent_id,
                        facility_id=facility_id,
                        purpose=purpose,
                        route=local_route,
                        route_points=route_points,
                        minute=world.minute_index,
                        origin_zone_id=destination_zone_id,
                    ):
                        return
                    self._reject_room_approach(world, event, agent_id, facility_id, origin_zone_id)
                    return
        if purpose == "toilet_request":
            try:
                self.toilet_service.request(
                    agent_id,
                    facility_id,
                    minute=world.minute_index,
                    validate_state=self.validate_request_state,
                )
            except ValueError as exc:
                raise EventApplicationError(str(exc)) from exc
        elif purpose == "resource_request":
            try:
                self.resource_service.request(
                    agent_id,
                    facility_id,
                    minute=world.minute_index,
                    validate_state=False,
                )
            except ValueError as exc:
                # Arrival has completed; failed admission leaves the Agent idle at the facility.
                resource_id = ResourceServiceManager.resource_for_facility(facility.kind)
                action = "fetch_water" if resource_id == "water_ml" else "collect_meal"
                self.resource_service.record_request_outcome(
                    agent_id=agent_id,
                    action=action,
                    outcome="rejected",
                    reason="request_rejected",
                    facility_id=facility_id,
                    origin_zone_id=origin_zone_id,
                    detail=str(exc),
                )
                world.metric_state["resource_service_request_rejected_count"] = (
                    world.metric_state.get("resource_service_request_rejected_count", 0.0) + 1.0
                )

    def _schedule_room_approach(
        self,
        world: WorldState,
        *,
        agent_id: str,
        facility_id: str,
        purpose: str,
        route: Any,
        route_points: tuple[Any, ...],
        minute: int,
        origin_zone_id: str,
    ) -> bool:
        due_minute = minute + route.travel_minutes
        if due_minute >= world.duration_minutes:
            return False
        sequence = world.allocate_event_sequence()
        task_id = f"agent-facility-approach-{sequence}"
        event_id = f"agent-facility-approach-complete-{sequence}"
        task = ActiveTask(
            id=task_id,
            kind="travel_to_toilet" if purpose == "toilet_request" else "travel_to_resource",
            agent_id=agent_id,
            resource_id=None,
            start_minute=minute,
            due_minute=due_minute,
        )
        event = ScheduledEvent(
            event_id=event_id,
            kind=EventKind.COMPLETION,
            due_minute=due_minute,
            priority=0,
            sequence=sequence,
            event_type="agent_travel_complete",
            payload={
                "agent_id": agent_id,
                "task_id": task_id,
                "purpose": purpose,
                "destination_zone_id": route.destination_zone_id,
                "facility_id": facility_id,
                "origin_zone_id": origin_zone_id,
                "route_zone_ids": list(route.zone_ids),
                "route_edge_ids": list(route.edge_ids),
                "travel_minutes": route.travel_minutes,
            },
            source_id="agent-facility-approach-v2",
            causation_id=None,
            provenance={
                "source_type": "assumption",
                "source_id": "ASM-SPATIAL-MOVEMENT-V2",
                "policy_version": "spatial-route-and-room-waypoint-v2",
            },
        )
        world.active_tasks[task_id] = task
        agent = world.agents[agent_id]
        agent.activity = Activity.MOVING
        agent.target_id = facility_id
        world.schedule_event(event)
        if MOVEMENT_STATE_KEY in world.model_state:
            start_movement(
                world,
                agent_id=agent_id,
                route=route,
                purpose=purpose,
                facility_id=facility_id,
                started_minute=minute,
                due_minute=due_minute,
                event_id=event_id,
                task_id=task_id,
                route_points=route_points,
            )
        return True

    def _reject_room_approach(
        self,
        world: WorldState,
        event: ScheduledEvent,
        agent_id: str,
        facility_id: str,
        origin_zone_id: Any,
    ) -> None:
        if self.resource_service is not None and event.payload.get("purpose") == "resource_request":
            facility = world.facilities.get(facility_id)
            resource_id = (
                ResourceServiceManager.resource_for_facility(facility.kind)
                if facility is not None
                else None
            )
            action = "fetch_water" if resource_id == "water_ml" else "collect_meal"
            self.resource_service.record_request_outcome(
                agent_id=agent_id,
                action=action,
                outcome="rejected",
                reason="room_route_blocked",
                facility_id=facility_id,
                origin_zone_id=origin_zone_id
                if isinstance(origin_zone_id, str)
                else world.agents[agent_id].zone_id,
            )
            world.metric_state["resource_service_request_rejected_count"] = (
                world.metric_state.get("resource_service_request_rejected_count", 0.0) + 1.0
            )
        world.metric_state["facility_approach_rejected_count"] = (
            world.metric_state.get("facility_approach_rejected_count", 0.0) + 1.0
        )


@dataclass(frozen=True)
class ResourceServiceCompletionHandler:
    """Apply a scheduled service completion through the resource service state machine."""

    service: ResourceServiceManager

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        if world.schema_version != 6:
            raise EventApplicationError("resource service completion requires WorldState v6")
        self.service.complete(event)


@dataclass(frozen=True)
class ResourceServiceCancellationHandler:
    """Apply an explicit typed cancellation with one-time reservation release."""

    service: ResourceServiceManager

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        if world.schema_version != 6:
            raise EventApplicationError("resource service cancellation requires WorldState v6")
        self.service.cancel(event)


@dataclass(frozen=True)
class DeliveryDelayHandler:
    """Reschedule one pending shipment without changing its stable identity."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        def apply(candidate: WorldState) -> None:
            if set(event.payload) != {"shipment_id", "to_minute"} or event.source_id is None:
                raise EventApplicationError("delivery delay payload fields or source are invalid")
            operational = candidate.model_state.get(OPERATIONAL_EVENTS_KEY)
            if operational is None:
                raise EventApplicationError("delivery delay requires operational_events_v1 state")
            shipment_id = event.payload.get("shipment_id")
            record = operational["shipments"].get(shipment_id)
            if record is None:
                raise EventApplicationError("delivery delay references an unknown shipment")
            target = next(
                (
                    pending
                    for pending in candidate.event_queue.pending()
                    if pending.event_id == record["event_id"]
                ),
                None,
            )
            to_minute = event.payload.get("to_minute")
            if (
                target is None
                or target.event_type != "resource_delivery"
                or target.payload.get("shipment_id") != shipment_id
                or target.due_minute != record["due_minute"]
            ):
                raise EventApplicationError("shipment is no longer pending for delay")
            if (
                not isinstance(to_minute, int)
                or isinstance(to_minute, bool)
                or target.due_minute <= candidate.minute_index
                or to_minute <= target.due_minute
                or to_minute <= candidate.minute_index
                or to_minute >= candidate.duration_minutes
            ):
                raise EventApplicationError(
                    "shipment delay must move a not-yet-due delivery forward"
                )
            replacement_provenance = target.to_dict()["provenance"]
            replacement_provenance["last_delay_event_id"] = event.event_id
            effect_marker = event.to_dict()["provenance"].get("effect_program_v1")
            if isinstance(effect_marker, Mapping):
                replacement_provenance["effect_program_v1"] = {
                    **dict(effect_marker),
                    "phase": "derived",
                }
            replacement = ScheduledEvent(
                event_id=target.event_id,
                kind=target.kind,
                due_minute=to_minute,
                priority=target.priority,
                sequence=target.sequence,
                event_type=target.event_type,
                payload=target.to_dict()["payload"],
                source_id=target.source_id,
                causation_id=event.event_id,
                provenance=replacement_provenance,
            )
            candidate.event_queue.replace(target.event_id, replacement)
            record["due_minute"] = to_minute

        _apply_operational_transaction(world, event, apply)


@dataclass(frozen=True)
class WaterOutageHandler:
    """Block new staffed water service at an explicitly named target."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        def apply(candidate: WorldState) -> None:
            if event.source_id is None or set(event.payload) not in (
                {"outage_id", "zone_id"},
                {"outage_id", "facility_id"},
            ):
                raise EventApplicationError("water outage payload fields or source are invalid")
            operational = candidate.model_state.get(OPERATIONAL_EVENTS_KEY)
            outage_id = event.payload.get("outage_id")
            record = operational["water_outages"].get(outage_id) if operational else None
            if record is None or record["outage_event_id"] != event.event_id:
                raise EventApplicationError("water outage does not match operational state")
            target_kind = "zone" if "zone_id" in event.payload else "facility"
            target_id = event.payload.get("zone_id", event.payload.get("facility_id"))
            if record["target_kind"] != target_kind or record["target_id"] != target_id:
                raise EventApplicationError("water outage target does not match operational state")
            if target_kind == "zone":
                if not any(
                    facility.kind == "water_station" and facility.zone_id == target_id
                    for facility in candidate.facilities.values()
                ):
                    raise EventApplicationError("water outage zone has no water_station")
            else:
                facility = candidate.facilities.get(target_id)
                if facility is None or facility.kind != "water_station":
                    raise EventApplicationError("water outage must target a water_station")
            if record["active"]:
                raise EventSuppressed("water outage is already active")
            record["active"] = True

        _apply_operational_transaction(world, event, apply)


@dataclass(frozen=True)
class WaterRestorationHandler:
    """Restore new water service starts for one correlated outage."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        def apply(candidate: WorldState) -> None:
            if event.source_id is None or set(event.payload) != {"outage_id"}:
                raise EventApplicationError(
                    "water restoration payload fields or source are invalid"
                )
            operational = candidate.model_state.get(OPERATIONAL_EVENTS_KEY)
            outage_id = event.payload.get("outage_id")
            record = operational["water_outages"].get(outage_id) if operational else None
            if record is None or record["restore_event_id"] != event.event_id:
                raise EventApplicationError("water restoration does not match its outage")
            if not record["active"]:
                raise EventSuppressed("water outage is not active")
            record["active"] = False

        _apply_operational_transaction(world, event, apply)


@dataclass(frozen=True)
class StaffUnavailabilityHandler:
    """Remove one staff member from assignment and safely interrupt active work."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> tuple[EventLogRecord, ...]:
        def apply(candidate: WorldState) -> tuple[EventLogRecord, ...]:
            additional_records: list[EventLogRecord] = []
            if event.source_id is None or set(event.payload) != {"staff_id", "until_minute"}:
                raise EventApplicationError(
                    "staff unavailability payload fields or source are invalid"
                )
            operational = candidate.model_state.get(OPERATIONAL_EVENTS_KEY)
            if operational is None:
                raise EventApplicationError("staff unavailability requires operational event state")
            staff_id = event.payload.get("staff_id")
            staff_member = candidate.staff.get(staff_id)
            until_minute = event.payload.get("until_minute")
            if staff_member is None:
                raise EventApplicationError("staff unavailability references unknown staff")
            if (
                not isinstance(until_minute, int)
                or isinstance(until_minute, bool)
                or until_minute <= candidate.minute_index
                or until_minute > candidate.duration_minutes
            ):
                raise EventApplicationError("staff unavailable until_minute is invalid")
            status = operational["staff_status"][staff_id]
            if status["unavailability_event_id"] == event.event_id:
                raise EventSuppressed("staff unavailability was already applied")

            restore_event_id: str | None = None
            if until_minute < candidate.duration_minutes:
                sequence = candidate.id_counters.get("event", 0)
                restore_event_id = f"staff-restored-{sequence}"
                restore = ScheduledEvent(
                    event_id=restore_event_id,
                    kind=EventKind.COMPLETION,
                    due_minute=until_minute,
                    priority=10,
                    sequence=sequence,
                    event_type="staff_restored",
                    payload={
                        "staff_id": staff_id,
                        "unavailability_event_id": event.event_id,
                    },
                    source_id=event.source_id,
                    causation_id=event.event_id,
                    provenance={
                        "source_type": "assumption",
                        "source_id": "ASM-H03-OPERATIONAL-EVENTS-V1",
                        "policy_version": "operational-events-v1",
                        **(
                            {
                                "effect_program_v1": {
                                    **event.to_dict()["provenance"]["effect_program_v1"],
                                    "phase": "derived",
                                }
                            }
                            if isinstance(event.provenance.get("effect_program_v1"), Mapping)
                            else {}
                        ),
                    },
                )
                _ensure_event_identity_free(candidate, restore)
                if candidate.allocate_event_sequence() != sequence:
                    raise EventApplicationError("event sequence changed during staff preflight")
                candidate.schedule_event(restore)

            support_tasks = [
                task
                for task in candidate.active_tasks.values()
                if task.staff_id == staff_id and task.kind == "staff_support"
            ]
            resource_tasks = [
                task
                for task in candidate.active_tasks.values()
                if task.staff_id == staff_id
                and task.kind in ("resource_service_water", "resource_service_food")
            ]
            operations = candidate.model_state.get("staff_operations_v1", {})
            operation_record = (
                operations.get("staff", {}).get(staff_id) if isinstance(operations, dict) else None
            )
            autonomous_operation = (
                operation_record is not None and operation_record.get("phase") != "idle"
            )
            if len(support_tasks) + len(resource_tasks) + int(autonomous_operation) > 1:
                raise EventApplicationError("staff owns multiple active service tasks")
            if resource_tasks:
                from sheltersim.domain.resource_service import ResourceServiceManager

                ResourceServiceManager(candidate).interrupt_for_staff(
                    staff_id, causation_event=event
                )
            elif support_tasks:
                additional_records.append(
                    _interrupt_support_task(candidate, support_tasks[0], staff_member, event)
                )
            elif autonomous_operation:
                from sheltersim.domain.staff_operations import interrupt_staff_operation

                interrupt_staff_operation(candidate, staff_id, event)

            status["available"] = False
            status["unavailability_event_id"] = event.event_id
            status["until_minute"] = until_minute
            status["restore_event_id"] = restore_event_id
            return tuple(additional_records)

        return _apply_operational_transaction(world, event, apply)


@dataclass(frozen=True)
class StaffRestorationHandler:
    """Restore staff only when the event still matches the latest absence token."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        def apply(candidate: WorldState) -> None:
            if event.source_id is None or set(event.payload) != {
                "staff_id",
                "unavailability_event_id",
            }:
                raise EventApplicationError(
                    "staff restoration payload fields or source are invalid"
                )
            operational = candidate.model_state.get(OPERATIONAL_EVENTS_KEY)
            staff_id = event.payload.get("staff_id")
            unavailable_id = event.payload.get("unavailability_event_id")
            record = operational["staff_status"].get(staff_id) if operational else None
            if record is None:
                raise EventApplicationError("staff restoration references unknown staff")
            if record["unavailability_event_id"] != unavailable_id:
                raise EventSuppressed("staff restoration belongs to an older unavailability")
            if record["restore_event_id"] != event.event_id:
                raise EventApplicationError("staff restoration event ID does not match its token")
            if record["available"]:
                raise EventSuppressed("staff is already available")
            record["available"] = True

        _apply_operational_transaction(world, event, apply)


def _interrupt_support_task(
    world: WorldState,
    task: object,
    staff_member: object,
    causation_event: ScheduledEvent,
) -> EventLogRecord:
    task_id = task.id
    request = next(
        (
            request
            for request in world.support_requests.values()
            if request.status is SupportRequestStatus.ASSIGNED and request.task_id == task_id
        ),
        None,
    )
    agent = world.agents.get(task.agent_id or "")
    completions = [
        pending
        for pending in world.event_queue.pending()
        if pending.event_type == "staff_support_complete"
        and pending.payload.get("task_id") == task_id
    ]
    if (
        request is None
        or agent is None
        or staff_member.active_task_id != task_id
        or staff_member.work_queue != [task_id]
        or len(completions) != 1
        or completions[0].due_minute != task.due_minute
    ):
        raise EventApplicationError("interrupted support task has inconsistent ownership")
    timeout_events = [
        pending
        for pending in world.event_queue.pending()
        if pending.event_type == "support_request_timeout"
        and pending.payload.get("request_id") == request.id
    ]
    if len(timeout_events) > 1 or (
        timeout_events and timeout_events[0].due_minute != request.timeout_minute
    ):
        raise EventApplicationError("support timeout state is inconsistent")
    if request.timeout_minute > world.minute_index and not timeout_events:
        sequence = world.id_counters.get("event", 0)
        timeout = ScheduledEvent(
            event_id=f"support-timeout-rescheduled-{sequence}",
            kind=EventKind.COMPLETION,
            due_minute=request.timeout_minute,
            priority=20,
            sequence=sequence,
            event_type="support_request_timeout",
            payload={"request_id": request.id, "agent_id": request.agent_id},
            source_id=request.id,
            causation_id=causation_event.event_id,
            provenance={
                "source_type": "assumption",
                "source_id": "ASM-STAFF-SUPPORT-LIFECYCLE-V1",
                "policy_version": STAFF_SUPPORT_POLICY_VERSION,
                "rescheduled_after_staff_unavailable": True,
            },
        )
        _ensure_event_identity_free(world, timeout)
        if world.allocate_event_sequence() != sequence:
            raise EventApplicationError("event sequence changed during support preflight")
        world.schedule_event(timeout)

    support_trips = [
        candidate
        for candidate in world.active_tasks.values()
        if candidate.kind == "staff_support_travel" and candidate.agent_id == agent.id
    ]
    if len(support_trips) > 1:
        raise EventApplicationError("resident has duplicate trips to assigned support")
    if support_trips:
        travel_task = support_trips[0]
        travel_events = [
            pending
            for pending in world.event_queue.pending()
            if pending.event_type == "agent_travel_complete"
            and pending.payload.get("task_id") == travel_task.id
            and pending.payload.get("purpose") == "staff_support_travel"
            and pending.payload.get("request_id") == request.id
        ]
        if len(travel_events) != 1 or travel_events[0].due_minute != travel_task.due_minute:
            raise EventApplicationError("resident support trip has inconsistent travel ownership")
        travel_event = world.event_queue.remove(travel_events[0].event_id)
        world.event_log.append(
            EventLogRecord(
                event=travel_event,
                realized_minute=world.minute_index,
                status=EventStatus.SUPPRESSED,
                detail="assigned staff became unavailable before resident arrival",
            )
        )
        del world.active_tasks[travel_task.id]
        movement = world.model_state.get(MOVEMENT_STATE_KEY)
        position = (
            movement.get("positions", {}).get(agent.id) if isinstance(movement, dict) else None
        )
        if isinstance(position, dict) and position.get("zone_id") in world.zones:
            agent.zone_id = position["zone_id"]
        finish_movement(world, agent.id)

    del world.active_tasks[task_id]
    staff_member.work_queue.clear()
    staff_member.active_task_id = None
    staff_member.busy_until = 0
    world.model_state[OPERATIONAL_EVENTS_KEY]["cancelled_staff_task_ids"].append(task_id)
    request.assigned_staff_id = None
    request.task_id = None
    if request.timeout_minute <= world.minute_index:
        cooldown = world.model_configs.get("staff", {}).get("support_cooldown_minutes", 30)
        request.status = SupportRequestStatus.UNRESOLVED
        request.resolved_minute = world.minute_index
        request.cooldown_until = world.minute_index + cooldown
        agent.activity = Activity.IDLE
        agent.target_id = None
        agent.support_request_id = None
        agent.support_cooldown_until = request.cooldown_until
        world.metric_state["support_unresolved_count"] = (
            world.metric_state.get("support_unresolved_count", 0.0) + 1.0
        )
    else:
        request.status = SupportRequestStatus.PENDING
        agent.activity = Activity.SEEKING_HELP
        agent.target_id = None
        agent.support_request_id = request.id
    return make_staff_support_continuity_record(
        world,
        request_id=request.id,
        agent_id=request.agent_id,
        task_id=task_id,
        previous_staff_id=staff_member.id,
        status=("requeued" if request.status is SupportRequestStatus.PENDING else "unresolved"),
        causation_id=causation_event.event_id,
        cause_type=(
            "staff_shift_change"
            if causation_event.event_type == "staff_shift_change"
            else "staff_unavailable"
        ),
    )


def _apply_operational_transaction(
    world: WorldState,
    event: ScheduledEvent,
    operation: Callable[[WorldState], Any],
) -> Any:
    """Run an H03 handler on a detached state and commit only after validation."""
    candidate = WorldState.from_dict(world.to_dict())
    result = operation(candidate)
    if isinstance(result, tuple) and result:
        if any(not isinstance(record, EventLogRecord) for record in result):
            raise EventApplicationError(
                "transactional follow-up records must be EventLogRecord values"
            )
        candidate.event_queue.remove(event.event_id)
        candidate.event_log.append(
            EventLogRecord(
                event=event,
                realized_minute=candidate.minute_index,
                status=EventStatus.APPLIED,
            )
        )
        for record in result:
            candidate.event_log.append(record)
        if "shared_operations" in candidate.model_configs:
            from pydantic import TypeAdapter

            from sheltersim.contracts.operations import StaffSupportContinuityReceipt
            from sheltersim.domain.common_operations import record_shared_operation

            for record in result:
                if record.event.event_type == "staff_support_continuity":
                    receipt = TypeAdapter(StaffSupportContinuityReceipt).validate_python(
                        record.event.payload
                    )
                    record_shared_operation(candidate, receipt)
        candidate.validate()
    else:
        candidate.validate(_in_flight_event=event)
    world.__dict__.clear()
    world.__dict__.update(candidate.__dict__)
    return result


def _ensure_event_identity_free(world: WorldState, event: ScheduledEvent) -> None:
    if (
        any(
            current.event_id == event.event_id or current.sequence == event.sequence
            for current in world.event_queue.pending()
        )
        or world.event_log.contains_id(event.event_id)
        or world.event_log.contains_sequence(event.sequence)
    ):
        raise EventApplicationError(f"generated event identity {event.event_id!r} is not unique")


@dataclass(frozen=True)
class StaffSupportCompletionHandler:
    """Finish a staff support task and start the Agent cooldown."""

    cooldown_minutes: int = 30
    require_colocated: bool = False

    def apply(self, world: WorldState, event: ScheduledEvent) -> tuple[EventLogRecord, ...] | None:
        values = tuple(
            event.payload.get(key) for key in ("request_id", "agent_id", "staff_id", "task_id")
        )
        if not all(isinstance(value, str) and value for value in values):
            raise EventApplicationError(
                "staff completion requires request, Agent, staff, and task IDs"
            )
        request_id, agent_id, staff_id, task_id = values
        task = world.active_tasks.get(task_id)
        operational = world.model_state.get(OPERATIONAL_EVENTS_KEY)
        if (
            task is None
            and operational is not None
            and task_id in operational["cancelled_staff_task_ids"]
        ):
            raise EventSuppressed(f"staff support task {task_id!r} was cancelled")
        if task is None or task.due_minute != event.due_minute:
            raise EventApplicationError("staff completion does not match an active due task")
        care_request = care_receipt_for_request(world, request_id)
        before: list[tuple[str, str, str, float, str]] = []
        agent = world.agents.get(agent_id)
        from sheltersim.policies.registry import create_care_outcome_formula

        care_formula = create_care_outcome_formula(world.model_configs)
        typed_care_deltas = (
            care_formula.support_state_deltas(
                care_request.get("support_kind", ""),
                agent_id=agent.id if agent is not None else None,
            )
            if care_formula is not None and care_request is not None
            else {}
        )
        relational_care_deltas: dict[str, dict[str, float]] = {}
        relational_context_source_ids: set[str] = set()
        if (
            care_request is not None
            and care_request.get("support_kind") == "caregiver_assistance"
            and care_formula is not None
        ):
            from sheltersim.domain.care_receipts import explicit_care_dependents

            relational_care_deltas = {
                dependent_id: care_formula.support_state_deltas(
                    "caregiver_assistance", agent_id=dependent_id
                )
                for dependent_id in explicit_care_dependents(
                    world,
                    agent_id,
                    household_id=care_request.get("household_id"),
                )
            }
            relational_care_deltas = {
                identity: deltas for identity, deltas in relational_care_deltas.items() if deltas
            }
            from sheltersim.contracts.population import population_context_people

            people_context = population_context_people(world)
            for identity in (agent_id, *relational_care_deltas):
                context = people_context.get(identity, {})
                sources = context.get("source_ids", ()) if isinstance(context, dict) else ()
                relational_context_source_ids.update(
                    source for source in sources if isinstance(source, str)
                )
        if care_request is not None and agent is not None:
            body_state = world.model_state.get("body_state_v1")
            body = (
                body_state.get("agents", {}).get(agent.id) if isinstance(body_state, dict) else None
            )
            if care_request.get("support_kind") == "emotional_support":
                before.extend(
                    (
                        ("agent", agent.id, "stress", agent.stress, "normalized_0_1"),
                        ("agent", agent.id, "trust", agent.trust, "normalized_0_1"),
                    )
                )
            if (
                isinstance(body_state, dict)
                and isinstance(body, dict)
                and (
                    care_request.get("support_kind") == "emotional_support"
                    or care_request.get("support_kind") == "hygiene_support"
                    or typed_care_deltas
                )
            ):
                for field in ("reassurance", "positive_affect", "negative_affect"):
                    if care_request.get("support_kind") == "emotional_support":
                        before.append(
                            (
                                "model_state",
                                agent.id,
                                f"body_state_v1.agents.{field}",
                                float(body[field]),
                                "normalized_0_1",
                            )
                        )
                if care_request.get("support_kind") == "hygiene_support":
                    before.append(
                        (
                            "model_state",
                            agent.id,
                            "body_state_v1.agents.hygiene_need",
                            float(body["hygiene_need"]),
                            "normalized_0_1",
                        )
                    )
                for path in typed_care_deltas:
                    if path == "agent.stress":
                        before.append(
                            (
                                "agent",
                                agent.id,
                                "stress",
                                float(agent.stress),
                                "normalized_0_1",
                            )
                        )
                    elif path.startswith("body_state_v1.agents."):
                        field = path.rsplit(".", 1)[-1]
                        before.append(
                            (
                                "model_state",
                                agent.id,
                                path,
                                float(body[field]),
                                "normalized_0_1",
                            )
                        )
        for dependent_id, deltas in relational_care_deltas.items():
            dependent = world.agents[dependent_id]
            dependent_body = world.model_state["body_state_v1"]["agents"][dependent_id]
            for path in deltas:
                if path == "agent.stress":
                    before.append(
                        (
                            "agent",
                            dependent_id,
                            "stress",
                            float(dependent.stress),
                            "normalized_0_1",
                        )
                    )
                elif path.startswith("body_state_v1.agents."):
                    field = path.rsplit(".", 1)[-1]
                    before.append(
                        (
                            "model_state",
                            dependent_id,
                            path,
                            float(dependent_body[field]),
                            "normalized_0_1",
                        )
                    )
        try:
            complete_support_task(
                world,
                request_id=request_id,
                agent_id=agent_id,
                staff_id=staff_id,
                task_id=task_id,
                minute=world.minute_index,
                cooldown_minutes=self.cooldown_minutes,
                require_colocated=self.require_colocated,
            )
        except ValueError as exc:
            raise EventApplicationError(str(exc)) from exc
        if care_request is None:
            return None

        observation = "not_measured"
        changes: list[NumericStateChange] = []
        care_outcome_source_id: str | None = None
        if agent is not None and (
            care_request.get("support_kind") in {"emotional_support", "hygiene_support"}
            or typed_care_deltas
        ):
            body_state = world.model_state.get("body_state_v1")
            body = (
                body_state.get("agents", {}).get(agent.id) if isinstance(body_state, dict) else None
            )
            config = body_state.get("config", {}) if isinstance(body_state, dict) else {}
            if isinstance(body, dict):
                support_kind = care_request.get("support_kind")
                if support_kind == "emotional_support":
                    relief = float(
                        config.get("complaint_reassurance_gain_by_agent", {}).get(
                            agent.id,
                            config.get("complaint_reassurance_gain", 0.0),
                        )
                    )
                    agent.stress = max(0.0, agent.stress - relief)
                    agent.trust = min(1.0, agent.trust + relief * 0.25)
                    body["reassurance"] = min(1.0, body["reassurance"] + relief)
                    body["positive_affect"] = min(1.0, body["positive_affect"] + relief * 0.5)
                    body["negative_affect"] = max(0.0, body["negative_affect"] - relief * 0.5)
                elif support_kind == "hygiene_support":
                    relief = float(
                        config.get("hygiene_support_relief_by_agent", {}).get(
                            agent.id,
                            config.get("hygiene_support_relief", 0.0),
                        )
                    )
                    from sheltersim.policies.registry import create_care_outcome_formula

                    care_formula = create_care_outcome_formula(world.model_configs)
                    if care_formula is None:
                        body["hygiene_need"] = max(0.0, body["hygiene_need"] - relief)
                    else:
                        body["hygiene_need"] = care_formula.hygiene_need_after_support(
                            float(body["hygiene_need"]), relief
                        )
                        care_outcome_source_id = care_formula.source_id
                elif typed_care_deltas and care_formula is not None:
                    for path, delta in typed_care_deltas.items():
                        if path == "agent.stress":
                            agent.stress = min(1.0, max(0.0, agent.stress + delta))
                        elif path.startswith("body_state_v1.agents."):
                            field = path.rsplit(".", 1)[-1]
                            body[field] = min(1.0, max(0.0, body[field] + delta))
                    if typed_care_deltas:
                        care_outcome_source_id = care_formula.source_id
                if care_formula is not None:
                    for dependent_id, deltas in relational_care_deltas.items():
                        dependent = world.agents[dependent_id]
                        dependent_body = body_state["agents"][dependent_id]
                        for path, delta in deltas.items():
                            if path == "agent.stress":
                                dependent.stress = min(1.0, max(0.0, dependent.stress + delta))
                            elif path.startswith("body_state_v1.agents."):
                                field = path.rsplit(".", 1)[-1]
                                dependent_body[field] = min(
                                    1.0, max(0.0, dependent_body[field] + delta)
                                )
                        if deltas:
                            care_outcome_source_id = care_formula.source_id
                after_values = {
                    (kind, identity, path): (
                        getattr(world.agents[identity], path)
                        if kind == "agent"
                        else world.model_state["body_state_v1"]["agents"][identity][
                            path.rsplit(".", 1)[-1]
                        ]
                    )
                    for kind, identity, path, _, _ in before
                }
                for kind, identity, path, previous, unit in before:
                    changes.append(
                        NumericStateChange(
                            entity_kind=kind,
                            entity_id=identity,
                            field_path=path,
                            before=previous,
                            after=after_values[(kind, identity, path)],
                            unit=unit,
                        )
                    )
                observation = (
                    "observed_change"
                    if any(change.before != change.after for change in changes)
                    else "observed_no_change"
                )

        care_source_ids = [
            "ASM-STAFF-SUPPORT-LIFECYCLE-V1",
            *sorted(relational_context_source_ids),
        ]
        if observation != "not_measured":
            care_source_ids.append("ASM-LIFE-STATE-FEEDBACK-V1")
        if care_outcome_source_id is not None:
            care_source_ids.append(care_outcome_source_id)
        care_event, _ = make_care_transition_record(
            world,
            request_id=request_id,
            agent_id=agent_id,
            household_id=care_request.get("household_id"),
            support_kind=care_request["support_kind"],
            status="completed",
            staff_id=staff_id,
            outcome_observation=observation,
            state_changes=tuple(changes),
            causation_id=event.event_id,
            source_ids=tuple(care_source_ids),
        )
        return (care_event,)


@dataclass(frozen=True)
class StaffOperationArrivalHandler:
    """Move a staff member to the assigned task after its actual route time."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        from sheltersim.domain.staff_operations import arrive_staff_operation

        if event.event_type != "staff_operation_arrive":
            raise EventApplicationError("staff operation arrival handler received another event")
        arrive_staff_operation(world, event)


@dataclass(frozen=True)
class StaffOperationReliefHandler:
    """Transfer an in-flight staff operation at an applied relief boundary."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> tuple[EventLogRecord, ...] | None:
        from sheltersim.domain.staff_operations import handoff_staff_operation

        receipt = handoff_staff_operation(world, event)
        return (receipt,) if receipt is not None else None


@dataclass(frozen=True)
class StaffOperationCompletionHandler:
    """Apply routed cleaning, repair, intake, outreach or patrol work."""

    def apply(self, world: WorldState, event: ScheduledEvent) -> tuple[EventLogRecord, ...] | None:
        task_kind = event.payload.get("task_kind")
        target_id = event.payload.get("target_id")
        target_zone_id = event.payload.get("target_zone_id")
        facility = world.facilities.get(target_id) if isinstance(target_id, str) else None
        previous_working_units = (
            facility.working_units if task_kind == "maintenance" and facility is not None else None
        )
        before = _staff_operation_measurements(
            world, task_kind=task_kind, target_id=target_id, target_zone_id=target_zone_id
        )
        from sheltersim.domain.staff_operations import complete_staff_operation

        if event.event_type != "staff_operation_complete":
            raise EventApplicationError("staff operation completion handler received another event")
        operation = complete_staff_operation(world, event)
        after = _staff_operation_measurements(
            world, task_kind=task_kind, target_id=target_id, target_zone_id=target_zone_id
        )
        state_changes = tuple(
            NumericStateChange(
                entity_kind=entity_kind,
                entity_id=entity_id,
                field_path=field_path,
                before=before[(entity_kind, entity_id, field_path)][0],
                after=value,
                unit=unit,
            )
            for (entity_kind, entity_id, field_path), (value, unit) in sorted(after.items())
            if (entity_kind, entity_id, field_path) in before
            and before[(entity_kind, entity_id, field_path)][0] != value
        )
        operation_event = make_staff_operation_record(
            world,
            operation=operation,
            state_changes=state_changes,
            outcome_measured=bool(before),
            causation_id=event.event_id,
        )
        followups = [operation_event[0]] if operation_event is not None else []
        if (
            previous_working_units is not None
            and facility is not None
            and facility.working_units != previous_working_units
        ):
            recovery_record = make_facility_recovery_record(
                world,
                facility_id=facility.id,
                component_id=event.payload["task_id"],
                status="restored",
                previous_working_units=previous_working_units,
                working_units=facility.working_units,
                causation_id=event.event_id,
                source_ids=tuple(value for value in (event.source_id,) if value),
            )
            if recovery_record is not None:
                followups.append(recovery_record[0])
        return tuple(followups) or None


def _staff_operation_measurements(
    world: WorldState,
    *,
    task_kind: Any,
    target_id: Any,
    target_zone_id: Any,
) -> dict[tuple[str, str, str], tuple[int | float, str]]:
    """Capture numeric outcome fields touched by one supported staff operation."""
    values: dict[tuple[str, str, str], tuple[int | float, str]] = {}

    def add(kind: str, entity_id: str, path: str, value: Any, unit: str) -> None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values[(kind, entity_id, path)] = (value, unit)

    if task_kind == "cleaning" and isinstance(target_zone_id, str):
        zone = world.model_state.get("staff_operations_v1", {}).get("zones", {}).get(target_zone_id)
        if isinstance(zone, dict):
            add(
                "model_state",
                target_zone_id,
                "staff_operations_v1.zones.sanitation",
                zone.get("sanitation"),
                "normalized_0_1",
            )
    elif task_kind == "maintenance" and isinstance(target_id, str):
        facility = world.facilities.get(target_id)
        if facility is not None:
            add("facility", facility.id, "working_units", facility.working_units, "facility_unit")
    elif task_kind in {"information_outreach", "patrol"} and isinstance(target_zone_id, str):
        body_agents = world.model_state.get("body_state_v1", {}).get("agents", {})
        for agent in sorted(world.agents.values(), key=lambda row: row.id):
            if agent.zone_id != target_zone_id:
                continue
            add("agent", agent.id, "trust", agent.trust, "normalized_0_1")
            body = body_agents.get(agent.id)
            if isinstance(body, dict):
                for field in ("uncertainty", "reassurance", "positive_affect"):
                    add(
                        "model_state",
                        agent.id,
                        f"body_state_v1.agents.{field}",
                        body.get(field),
                        "normalized_0_1",
                    )
    return values


@dataclass(frozen=True)
class SupportRequestTimeoutHandler:
    """Close a pending request at its deadline; assigned work is not interrupted."""

    cooldown_minutes: int = 30

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        request_id = event.payload.get("request_id")
        agent_id = event.payload.get("agent_id")
        if not isinstance(request_id, str) or not isinstance(agent_id, str):
            raise EventApplicationError("support timeout requires request_id and agent_id")
        try:
            changed = timeout_support_request(
                world,
                request_id=request_id,
                agent_id=agent_id,
                minute=world.minute_index,
                cooldown_minutes=self.cooldown_minutes,
            )
        except ValueError as exc:
            raise EventApplicationError(str(exc)) from exc
        if not changed:
            raise EventSuppressed("support request is no longer pending")
