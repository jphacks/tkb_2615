"""The dispatch phase: apply what is due, then what follows from the applied events."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sheltersim.core.world import WorldState
from sheltersim.domain.action_lifecycle import (
    record_applied_action_completion,
    record_applied_population_exit,
)
from sheltersim.domain.agent_dynamics import learn_facility
from sheltersim.domain.care_receipts import (
    append_applied_care_transition,
    care_receipt_for_request,
    care_source_ids_for_agent,
    ingest_applied_shared_operation_records,
)
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
from sheltersim.domain.models import (
    Activity,
    AgentInformationState,
    InformationBeliefStatus,
    InformationTruthStatus,
    SupportRequestStatus,
    TypedMessage,
)
from sheltersim.domain.movement import MOVEMENT_STATE_KEY, finish_movement
from sheltersim.domain.population import admit_pending_arrivals
from sheltersim.domain.population_lifecycle import current_household_id
from sheltersim.domain.staff_operations import interrupt_staff_trips_on_closed_routes
from sheltersim.events.models import EventStatus, ScheduledEvent


class DispatchPhase:
    """Steps of TickPhase.DISPATCH_DUE_EVENTS, a mixin of OfflineSimulationController."""

    def _run_dispatch_phase(self, world: WorldState, minute: int) -> None:
        outcomes = self._dispatch_due_events(world, minute)
        self._record_applied_outcomes(world, outcomes, minute)
        self._refresh_space_after_geometry_change(outcomes)
        self._interrupt_trips_on_closed_routes(world, outcomes)
        self._admit_pending_returns(world, minute)
        self._sync_movement_after_events()
        admit_pending_arrivals(world, minute)
        self._consider_social_contact_events(outcomes)

    def _dispatch_due_events(self, world: WorldState, minute: int) -> tuple[Any, ...]:
        return self.event_dispatcher.dispatch_due(
            world,
            minute,
            after_event=lambda records: ingest_applied_shared_operation_records(world, records),
        )

    def _dispatch_one_event(
        self, world: WorldState, event: ScheduledEvent, minute: int
    ) -> list[Any]:
        """What native/src/rules/dispatch.rs calls for an event it has no Rust function for."""
        return self.event_dispatcher.dispatch_one(
            world,
            event,
            minute,
            after_event=lambda records: ingest_applied_shared_operation_records(world, records),
        )

    def _record_applied_outcomes(
        self, world: WorldState, outcomes: tuple[Any, ...], minute: int
    ) -> None:
        for outcome in outcomes:
            if outcome.status is EventStatus.APPLIED:
                record_applied_action_completion(
                    world, outcome.event, minute, active_actions=self.active_actions
                )
                record_applied_population_exit(
                    world, outcome.event, minute, active_actions=self.active_actions
                )
                if outcome.event.event_type == "agent_travel_complete":
                    agent_id = outcome.event.payload.get("agent_id")
                    facility_id = outcome.event.payload.get("facility_id")
                    if isinstance(agent_id, str) and isinstance(facility_id, str):
                        learn_facility(world, agent_id, facility_id)
                    if outcome.event.payload.get("purpose") == "staff_support_travel":
                        self._record_staff_support_started(world, outcome.event)
                if outcome.event.event_type == "support_request_timeout":
                    self._record_request_interrupted(world, outcome.event)

    def _record_staff_support_started(self, world: WorldState, event: ScheduledEvent) -> None:
        request_id = event.payload.get("request_id")
        request = world.support_requests.get(request_id)
        prior = care_receipt_for_request(world, request_id)
        # A request keeps the household it was made in, as its
        # completion does; a later reunion must not change it (#179).
        if (
            request is not None
            and prior is not None
            and request.status is SupportRequestStatus.ASSIGNED
            and request.assigned_staff_id == event.payload.get("staff_id")
            and world.agents[request.agent_id].activity is Activity.SEEKING_HELP
        ):
            append_applied_care_transition(
                world,
                request_id=request.id,
                agent_id=request.agent_id,
                household_id=prior.get("household_id"),
                support_kind=prior["support_kind"],
                status="started",
                staff_id=request.assigned_staff_id,
                causation_id=request.task_id,
                source_ids=care_source_ids_for_agent(world, request.agent_id),
            )

    def _record_request_interrupted(self, world: WorldState, event: ScheduledEvent) -> None:
        request_id = event.payload.get("request_id")
        request = world.support_requests.get(request_id)
        prior = care_receipt_for_request(world, request_id) if isinstance(request_id, str) else None
        if (
            request is not None
            and prior is not None
            and request.status is SupportRequestStatus.UNRESOLVED
        ):
            append_applied_care_transition(
                world,
                request_id=request.id,
                agent_id=request.agent_id,
                household_id=prior.get("household_id"),
                support_kind=prior["support_kind"],
                status="interrupted",
                causation_id=event.event_id,
                source_ids=care_source_ids_for_agent(world, request.agent_id),
            )

    def _refresh_space_after_geometry_change(self, outcomes: tuple[Any, ...]) -> None:
        geometry_changed = any(
            outcome.status is EventStatus.APPLIED
            and outcome.event.event_type
            in {
                "effect_program_geometry",
                "effect_program_geometry_expire",
                "effect_program_structure",
            }
            for outcome in outcomes
        )
        if geometry_changed:
            self._refresh_space()

    def _interrupt_trips_on_closed_routes(
        self, world: WorldState, outcomes: tuple[Any, ...]
    ) -> None:
        for outcome in outcomes:
            if outcome.status is EventStatus.APPLIED:
                self._interrupt_trips_of_event(world, outcome.event)

    def _interrupt_trips_of_event(self, world: WorldState, event: ScheduledEvent) -> None:
        parameters = event.payload.get("parameters", {})
        if (
            event.event_type == "effect_program_geometry"
            and event.payload.get("op") == "set_route_access"
            and isinstance(parameters, Mapping)
            and parameters.get("accessible") is False
        ):
            interrupt_staff_trips_on_closed_routes(world, event)

    def _admit_pending_returns(self, world: WorldState, minute: int) -> None:
        from sheltersim.domain.population_lifecycle import admit_pending_returns

        admit_pending_returns(world, minute)

    def _sync_movement_after_events(self) -> None:
        """Finish routes when their typed travel-completion event changed Agent state."""
        state = self.world.model_state.get(MOVEMENT_STATE_KEY)
        if state is None:
            return
        for agent_id in sorted(tuple(state["journeys"])):
            agent = self.world.agents.get(agent_id)
            journey = state["journeys"].get(agent_id, {})
            queue_trip_is_active = (
                agent is not None
                and agent.activity is Activity.QUEUED
                and journey.get("purpose")
                in {
                    "toilet_queue_position",
                    "resource_queue_position",
                    "toilet_queue_service",
                    "resource_queue_service",
                }
            )
            if agent is None or (
                agent.activity is not Activity.MOVING and not queue_trip_is_active
            ):
                self._finish_stopped_journey(agent_id)

    def _finish_stopped_journey(self, agent_id: str) -> None:
        state = self.world.model_state[MOVEMENT_STATE_KEY]
        agent = self.world.agents.get(agent_id)
        journey = state["journeys"].get(agent_id, {})
        bed_point = None
        if journey.get("purpose") == "sleep_bed":
            overlay = self.world.model_state.get("geometry_overlay_v1", {})
            reservation = overlay.get("bed_reservations", {}).get(agent_id, {})
            room = overlay.get("temporary_rooms", {}).get(reservation.get("room_id"), {})
            bed_points = room.get("bed_points_m", [])
            index = reservation.get("bed_index")
            if type(index) is int and 0 <= index < len(bed_points):
                bed_point = bed_points[index]
        finish_movement(self.world, agent_id)
        if bed_point is not None and agent is not None:
            state["positions"][agent_id].update(
                {
                    "zone_id": agent.zone_id,
                    "point": bed_point,
                    "edge_id": None,
                    "progress": 1.0,
                    "position_kind": "physical",
                }
            )

    def _consider_social_contact_events(self, outcomes: tuple[Any, ...]) -> None:
        """Use only successfully applied local contacts as social interaction triggers."""
        for outcome in outcomes:
            if outcome.status is EventStatus.APPLIED:
                self._consider_social_contact_event(outcome.event)

    def _consider_social_contact_event(self, event: ScheduledEvent) -> None:
        if self.social_policy is None or event.event_type != "information_contact_update":
            return
        sender_id = event.payload.get("sender_agent_id")
        recipient_id = event.payload.get("recipient_agent_id")
        message_id = event.payload.get("message_id")
        if not all(isinstance(value, str) for value in (sender_id, recipient_id)):
            return
        sender = self.world.agents.get(sender_id)
        recipient = self.world.agents.get(recipient_id)
        if sender is None or recipient is None or sender_id == recipient_id:
            return
        if (
            current_household_id(self.world, sender.id) is not None
            and current_household_id(self.world, sender.id)
            == current_household_id(self.world, recipient.id)
            and max(sender.stress, recipient.stress) >= self.social_policy.limits.stress_threshold
        ):
            self.social_policy.consider_cooperation(
                self.world,
                initiator_agent_id=sender_id,
                participant_agent_id=recipient_id,
                trigger_event_id=event.event_id,
            )
        state = self.world.model_state.get(INFORMATION_SPREAD_STATE_KEY)
        if not isinstance(message_id, str) or not isinstance(state, dict):
            return
        raw_message = state["messages"].get(message_id)
        raw_belief = state["beliefs"].get(recipient_id, {}).get(message_id)
        if raw_message is None or raw_belief is None:
            return
        message = TypedMessage.from_dict(raw_message)
        belief = AgentInformationState.from_dict(raw_belief)
        if (
            message.truth_status is InformationTruthStatus.FALSE
            and belief.belief is InformationBeliefStatus.HEARD
        ):
            self.social_policy.consider_information_reaction(
                self.world,
                agent_id=recipient_id,
                participant_agent_id=sender_id,
                message_id=message_id,
                trigger_event_id=event.event_id,
            )

    def _schedule_return_to_initial_zone(
        self, world: WorldState, service_event: ScheduledEvent
    ) -> None:
        agent_id = service_event.payload.get("agent_id")
        if not isinstance(agent_id, str) or agent_id not in world.agents:
            return
        agent = world.agents[agent_id]
        destination = agent.profile.initial_zone_id
        if agent.zone_id == destination:
            return
        route = self._route(agent.zone_id, destination, agent.id)
        if route is None:
            return
        self._schedule_travel(
            agent_id=agent.id,
            route=route,
            purpose="return",
            facility_id=None,
            minute=world.minute_index,
            causation_id=service_event.event_id,
            validate_state=False,
        )
