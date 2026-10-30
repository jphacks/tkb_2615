"""The propose phase: each due decision, and the trips and requests it starts.

The other phases plan and schedule trips with the route and travel methods here too.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from sheltersim.core.rng import KeyedRandom
from sheltersim.core.world import WorldState
from sheltersim.domain.action_lifecycle import record_action_proposal
from sheltersim.domain.agent_dynamics import agent_knows_facility
from sheltersim.domain.agent_dynamics import record_proposal as record_agent_proposal
from sheltersim.domain.care_receipts import (
    append_applied_care_transition,
    care_source_ids_for_agent,
    support_kind_for_agent,
)
from sheltersim.domain.facility_queues import facility_has_independent_queues
from sheltersim.domain.geometry_overlay import (
    GEOMETRY_OVERLAY_KEY,
    parsed_facility_geometry,
    release_sleep_bed,
    reserve_sleep_bed,
)
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
from sheltersim.domain.models import ActiveTask, Activity, SupportRequestStatus
from sheltersim.domain.movement import (
    MOVEMENT_STATE_KEY,
    facility_approach_route,
    route_is_physically_clear,
    route_with_movement_time,
    start_movement,
)
from sheltersim.domain.population_lifecycle import current_household_id
from sheltersim.domain.social_dialogue import capture_social_state_context
from sheltersim.domain.temporary_spaces import incoming_travelers
from sheltersim.domain.zones import ZoneGraph
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.llm.contracts import LLMStage, LLMTask
from sheltersim.llm.runtime import LEGACY_SUPPORT_PRIORITY_POLICY_VERSION, support_priority_runtime
from sheltersim.llm.schemas import SUPPORT_ADVICE_SCHEMA
from sheltersim.policies.behavior import ActionKind
from sheltersim.policies.space import SpaceRouter, ZoneRoute
from sheltersim.policies.world_view import AgentView, WorldView


class ProposePhase:
    """Steps of TickPhase.PROPOSE_ACTIONS, a mixin of OfflineSimulationController."""

    def _run_propose_phase(self, world: WorldState, minute: int, rng: KeyedRandom) -> None:
        view = self._view_for_proposals(world, minute)
        self._apply_due_proposals(view, rng)

    def _view_for_proposals(self, world: WorldState, minute: int) -> WorldView:
        prior, self._view_before_proposals = self._view_before_proposals, None
        return (
            WorldView.after_state_update(prior, world)
            if prior is not None and prior.minute_index == minute
            else WorldView.from_world(
                world,
                person_cache=self._person_contexts,
                environment_cache=self._zone_environments,
            )
        )

    def _apply_due_proposals(self, view: WorldView, rng: KeyedRandom) -> None:
        for proposal in self.decisions.propose_due(view, rng):
            self._apply_proposal(
                proposal,
                observed_state=_agent_view_snapshot(view.agents[proposal.agent_id]),
            )

    def _apply_proposal(
        self,
        proposal: Any,
        *,
        observed_state: dict[str, Any] | None = None,
    ) -> None:
        agent = self.world.agents[proposal.agent_id]
        prior_activity = agent.activity
        agent.next_decision_at = proposal.next_decision_at
        target_id: str | None = None

        if proposal.action is not ActionKind.SLEEP:
            release_sleep_bed(self.world, agent.id)

        if proposal.action is ActionKind.WAIT:
            agent.activity = Activity.IDLE
            agent.target_id = None
        elif proposal.action is ActionKind.REST:
            agent.activity = Activity.RESTING
            agent.target_id = None
        elif proposal.action is ActionKind.SLEEP:
            reservation = reserve_sleep_bed(self.world, agent.id)
            if reservation is not None and reservation["travel_minutes"] > 0:
                zone_id = agent.zone_id
                route = ZoneRoute(
                    origin_zone_id=zone_id,
                    destination_zone_id=zone_id,
                    zone_ids=(zone_id, zone_id),
                    edge_ids=(),
                    travel_minutes=reservation["travel_minutes"],
                )
                scheduled = self._schedule_travel(
                    agent_id=agent.id,
                    route=route,
                    route_points=reservation["route_points"],
                    purpose="sleep_bed",
                    facility_id=None,
                    minute=self.world.minute_index,
                    validate_state=False,
                )
                if scheduled:
                    target_id = reservation["target_id"]
                else:
                    release_sleep_bed(self.world, agent.id)
                    agent.activity = Activity.SLEEPING
                    agent.target_id = None
            else:
                if reservation is not None:
                    movement = self.world.model_state.get(MOVEMENT_STATE_KEY, {})
                    position = movement.get("positions", {}).get(agent.id)
                    if isinstance(position, dict):
                        position.update(
                            {
                                "zone_id": agent.zone_id,
                                "point": reservation["point"].model_dump(mode="json"),
                                "edge_id": None,
                                "progress": 1.0,
                                "position_kind": "physical",
                            }
                        )
                    target_id = reservation["target_id"]
                agent.activity = Activity.SLEEPING
                agent.target_id = None
        elif proposal.action in (ActionKind.SEEK_HELP, ActionKind.SEEK_INFORMATION):
            try:
                receipt = self.staff_service.request(
                    agent.id,
                    minute=self.world.minute_index,
                    validate_state=False,
                )
                target_id = receipt.request_id
                request = self.world.support_requests[receipt.request_id]
                if proposal.action is ActionKind.SEEK_HELP:
                    append_applied_care_transition(
                        self.world,
                        request_id=request.id,
                        agent_id=agent.id,
                        household_id=current_household_id(self.world, agent.id),
                        support_kind=support_kind_for_agent(self.world, agent.id),
                        status="requested",
                        causation_id=None,
                        source_ids=care_source_ids_for_agent(self.world, agent.id),
                    )
                    if request.status is SupportRequestStatus.UNRESOLVED:
                        append_applied_care_transition(
                            self.world,
                            request_id=request.id,
                            agent_id=agent.id,
                            household_id=current_household_id(self.world, agent.id),
                            support_kind=support_kind_for_agent(self.world, agent.id),
                            status="interrupted",
                            source_ids=care_source_ids_for_agent(self.world, agent.id),
                        )
                if self.information_policy is not None and (
                    proposal.action is ActionKind.SEEK_INFORMATION
                    or self.world.model_state[INFORMATION_SPREAD_STATE_KEY]["query_pressure"][
                        agent.id
                    ]
                    >= self.information_policy.config.query_threshold
                ):
                    self.information_policy.record_support_request(
                        self.world.model_state[INFORMATION_SPREAD_STATE_KEY],
                        agent_id=agent.id,
                        request_id=receipt.request_id,
                        minute=self.world.minute_index,
                    )
                if self.llm_router is not None and request.status.value == "pending":
                    self._prioritize_support_request(receipt.request_id, proposal)
                if self.social_policy is not None and request.status.value == "pending":
                    self.social_policy.consider_support_request(
                        self.world,
                        agent_id=agent.id,
                        request_id=receipt.request_id,
                    )
            except ValueError:
                self.world.metric_state["support_request_rejected_count"] = (
                    self.world.metric_state.get("support_request_rejected_count", 0.0) + 1.0
                )
        elif proposal.action in (
            ActionKind.SOCIALIZE,
            ActionKind.CARE_FAMILY,
            ActionKind.HELP_OTHERS,
        ):
            participant_id = proposal.target_id
            participant = self.world.agents.get(participant_id or "")
            interaction_minutes = self.world.model_configs.get("body_dynamics", {}).get(
                "social_interaction_minutes", 5
            )
            due_minute = self.world.minute_index + interaction_minutes
            interaction_data = {
                ActionKind.SOCIALIZE: (
                    "reassurance_exchange",
                    "offer_reassurance",
                    "emotional_check_in",
                ),
                ActionKind.CARE_FAMILY: ("family_care", "provide_family_support", "family_care"),
                ActionKind.HELP_OTHERS: ("voluntary_support", "offer_practical_help", "mutual_aid"),
            }[proposal.action]
            if (
                participant is not None
                and participant.id != agent.id
                and participant.zone_id == agent.zone_id
                and due_minute < self.world.duration_minutes
            ):
                interaction_type, action, topic_id = interaction_data
                sequence = self.world.allocate_event_sequence()
                self.world.schedule_event(
                    ScheduledEvent(
                        event_id=f"autonomous-social-action-{sequence}",
                        kind=EventKind.ENDOGENOUS,
                        due_minute=due_minute,
                        priority=35,
                        sequence=sequence,
                        event_type="social_interaction",
                        payload={
                            "interaction_type": interaction_type,
                            "agent_id": agent.id,
                            "participant_agent_id": participant.id,
                            "conversation_state_context": capture_social_state_context(
                                self.world, (agent.id, participant.id)
                            ),
                            "zone_id": agent.zone_id,
                            "topic_id": topic_id,
                            "action": action,
                            "reason_short": "autonomous_need_choice",
                            "interaction_duration_minutes": interaction_minutes,
                        },
                        source_id="event-triggered-social-v2",
                        provenance={
                            "source_type": "assumption",
                            "source_id": "ASM-EVENT-TRIGGERED-SOCIAL-INTERACTIONS-V1",
                            "policy_version": "event-triggered-social-v2",
                        },
                    )
                )
                target_id = participant.id
                # A social/care action occupies this decision slot until its
                # typed completion event. The actor can reconsider immediately
                # after the interaction is applied at that boundary.
                agent.next_decision_at = due_minute
        elif proposal.action is ActionKind.REQUEST_TOILET:
            facility_id = self._select_toilet(agent.id)
            if facility_id is not None:
                facility = self.world.facilities[facility_id]
                if agent.zone_id == facility.zone_id:
                    if facility_has_independent_queues(self.world, facility_id):
                        self.toilet_service.request(agent.id, facility_id, validate_state=False)
                        target_id = facility_id
                    else:
                        room_route = self._facility_approach(agent.id, facility_id)
                        if room_route is not None:
                            route, route_points = room_route
                        else:
                            route, route_points = None, ()
                        if route is not None and route.travel_minutes > 0:
                            if self._schedule_travel(
                                agent_id=agent.id,
                                route=route,
                                route_points=route_points,
                                purpose="toilet_request",
                                facility_id=facility_id,
                                minute=self.world.minute_index,
                                validate_state=False,
                            ):
                                target_id = facility_id
                        elif route is not None:
                            self.toilet_service.request(agent.id, facility_id, validate_state=False)
                            target_id = facility_id
                else:
                    route = self._route(agent.zone_id, facility.zone_id, agent.id)
                    if route is not None and self._schedule_travel(
                        agent_id=agent.id,
                        route=route,
                        purpose="toilet_request",
                        facility_id=facility_id,
                        minute=self.world.minute_index,
                        validate_state=False,
                    ):
                        target_id = facility_id

        elif proposal.action in (ActionKind.FETCH_WATER, ActionKind.COLLECT_MEAL):
            if self.resource_service is None:
                self.world.metric_state["resource_service_request_rejected_count"] = (
                    self.world.metric_state.get("resource_service_request_rejected_count", 0.0)
                    + 1.0
                )
            else:
                resource_id = (
                    "water_ml" if proposal.action is ActionKind.FETCH_WATER else "food_meal_units"
                )
                action = (
                    "fetch_water" if proposal.action is ActionKind.FETCH_WATER else "collect_meal"
                )
                facility_id, route, rejection_reason = self._select_resource_facility(
                    agent.id, resource_id
                )
                if rejection_reason is not None:
                    self.resource_service.record_request_outcome(
                        agent_id=agent.id,
                        action=action,
                        outcome="rejected",
                        reason=rejection_reason,
                        facility_id=facility_id,
                        origin_zone_id=agent.zone_id,
                    )
                    self.world.metric_state["resource_service_request_rejected_count"] = (
                        self.world.metric_state.get("resource_service_request_rejected_count", 0.0)
                        + 1.0
                    )
                elif facility_id is not None and route is not None:
                    facility = self.world.facilities[facility_id]
                    if agent.zone_id == facility.zone_id:
                        if facility_has_independent_queues(self.world, facility_id):
                            try:
                                self.resource_service.request(
                                    agent.id, facility_id, validate_state=False
                                )
                                target_id = facility_id
                            except ValueError as exc:
                                self.resource_service.record_request_outcome(
                                    agent_id=agent.id,
                                    action=action,
                                    outcome="rejected",
                                    reason="request_rejected",
                                    facility_id=facility_id,
                                    origin_zone_id=agent.zone_id,
                                    detail=str(exc),
                                )
                                self.world.metric_state[
                                    "resource_service_request_rejected_count"
                                ] = (
                                    self.world.metric_state.get(
                                        "resource_service_request_rejected_count", 0.0
                                    )
                                    + 1.0
                                )
                        else:
                            room_route = self._facility_approach(agent.id, facility_id)
                            if room_route is None:
                                self.resource_service.record_request_outcome(
                                    agent_id=agent.id,
                                    action=action,
                                    outcome="rejected",
                                    reason="room_route_blocked",
                                    facility_id=facility_id,
                                    origin_zone_id=agent.zone_id,
                                )
                            else:
                                local_route, route_points = room_route
                                if local_route.travel_minutes > 0:
                                    if self._schedule_travel(
                                        agent_id=agent.id,
                                        route=local_route,
                                        route_points=route_points,
                                        purpose="resource_request",
                                        facility_id=facility_id,
                                        minute=self.world.minute_index,
                                        validate_state=False,
                                    ):
                                        target_id = facility_id
                                        self.resource_service.record_request_outcome(
                                            agent_id=agent.id,
                                            action=action,
                                            outcome="travel_scheduled",
                                            reason="in_room_route_available",
                                            facility_id=facility_id,
                                            origin_zone_id=agent.zone_id,
                                        )
                                else:
                                    try:
                                        self.resource_service.request(
                                            agent.id,
                                            facility_id,
                                            validate_state=False,
                                        )
                                        target_id = facility_id
                                    except ValueError as exc:
                                        self.resource_service.record_request_outcome(
                                            agent_id=agent.id,
                                            action=action,
                                            outcome="rejected",
                                            reason="request_rejected",
                                            facility_id=facility_id,
                                            origin_zone_id=agent.zone_id,
                                            detail=str(exc),
                                        )
                                        self.world.metric_state[
                                            "resource_service_request_rejected_count"
                                        ] = (
                                            self.world.metric_state.get(
                                                "resource_service_request_rejected_count", 0.0
                                            )
                                            + 1.0
                                        )
                    else:
                        if self._schedule_travel(
                            agent_id=agent.id,
                            route=route,
                            purpose="resource_request",
                            facility_id=facility_id,
                            minute=self.world.minute_index,
                            validate_state=False,
                        ):
                            target_id = facility_id
                            self.resource_service.record_request_outcome(
                                agent_id=agent.id,
                                action=action,
                                outcome="travel_scheduled",
                                reason="route_available",
                                facility_id=facility_id,
                                origin_zone_id=agent.zone_id,
                            )
                        else:
                            self.resource_service.record_request_outcome(
                                agent_id=agent.id,
                                action=action,
                                outcome="rejected",
                                reason="travel_rejected",
                                facility_id=facility_id,
                                origin_zone_id=agent.zone_id,
                                detail="travel could not be scheduled within current limits",
                            )
                            self.world.metric_state["resource_service_request_rejected_count"] = (
                                self.world.metric_state.get(
                                    "resource_service_request_rejected_count", 0.0
                                )
                                + 1.0
                            )

        recorded_proposal = proposal.with_outcome(target_id, agent.next_decision_at)
        self.artifacts.record_decision(
            recorded_proposal,
            observed_state=observed_state,
        )
        record_action_proposal(
            self.world,
            recorded_proposal,
            target_id=target_id,
            committed=(
                target_id is not None
                or proposal.action in {ActionKind.WAIT, ActionKind.REST, ActionKind.SLEEP}
                or agent.activity is not prior_activity
            )
            and not (
                proposal.action in {ActionKind.SEEK_HELP, ActionKind.SEEK_INFORMATION}
                and target_id in self.world.support_requests
                and self.world.support_requests[target_id].status is SupportRequestStatus.UNRESOLVED
            ),
            active_actions=self.active_actions,
        )
        if "agent_dynamics_v1" in self.world.model_state:
            record_agent_proposal(self.world, recorded_proposal, target_id=target_id)

    def _prioritize_support_request(self, request_id: str, proposal: Any) -> None:
        if self.llm_router is None or self.llm_router.mode.value == "offline":
            return
        agent = self.world.agents[proposal.agent_id]
        constraints = [
            "Use only the functional need and current state values below.",
            "Do not infer from names, demographics, or group membership.",
            "Do not approve or deny support; this only orders a staff queue.",
            "Return the required JSON object only.",
        ]
        if self.support_priority_version != LEGACY_SUPPORT_PRIORITY_POLICY_VERSION:
            # v2 asks only for stressed requesters within a daily cap; skipped
            # requests keep the rule priority 0, exactly like a fallback.
            runtime = support_priority_runtime(self.world)
            day_key = str(self.world.minute_index // 1440)
            skip_reason = (
                "below_stress_threshold"
                if agent.stress < self.support_priority_limits.stress_threshold
                else "global_daily_cap"
                if runtime["attempts_by_day"].get(day_key, 0)
                >= self.support_priority_limits.global_per_day
                else None
            )
            if skip_reason is not None:
                skipped = runtime["skipped_by_reason"]
                skipped[skip_reason] = skipped.get(skip_reason, 0) + 1
                return
            attempts = runtime["attempts_by_day"]
            attempts[day_key] = attempts.get(day_key, 0) + 1
            constraints.append("Keep rationale to one short phrase of at most 80 characters.")
        prompt = {
            "task": "Set queue priority for one synthetic staff support request.",
            "allowed_priority": {"0": "normal", "1": "urgent"},
            "constraints": constraints,
            "minute": self.world.minute_index,
            "support_need": agent.profile.support_need,
            "stress": agent.stress,
            "trust": agent.trust,
            "needs": {
                "toilet_urgency": agent.needs.toilet_urgency,
                "hunger": agent.needs.hunger,
                "fatigue": agent.needs.fatigue,
            },
        }
        input_text = json.dumps(
            prompt,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        response = self.llm_router.complete(
            LLMTask(
                stage=LLMStage.RUNTIME,
                input_text=input_text,
                state_hash=proposal.observed_state_hash,
                prompt_version=self.support_priority_version,
                schema_version=1,
                output_schema=SUPPORT_ADVICE_SCHEMA,
                fallback_text='{"priority":0,"rationale":"rule-based fallback"}',
                max_output_tokens=96,
            )
        )
        result = json.loads(response)
        self.world.model_state.setdefault("llm", {}).setdefault("support_priorities", {})[
            request_id
        ] = result["priority"]

    def _select_toilet(self, agent_id: str) -> str | None:
        agent = self.world.agents[agent_id]
        candidates: list[tuple[int, int, str]] = []
        for facility in self.world.facilities.values():
            if "toilet" not in facility.kind.casefold():
                continue
            if not agent_knows_facility(self.world, agent_id, facility.id):
                continue
            if not self._facility_queue_has_capacity(facility.id):
                continue
            route = self._route(agent.zone_id, facility.zone_id, agent_id)
            if route is None:
                continue
            in_transit = sum(
                1
                for other in self.world.agents.values()
                if other.activity is Activity.MOVING and other.target_id == facility.id
            )
            load = len(facility.queue_agent_ids) + len(facility.active_agent_ids) + in_transit
            candidates.append((load, route.travel_minutes, facility.id))
        return min(candidates)[2] if candidates else None

    def _select_resource_facility(
        self, agent_id: str, resource_id: str
    ) -> tuple[str | None, ZoneRoute | None, str | None]:
        agent = self.world.agents[agent_id]
        facilities = [
            facility
            for facility in self.world.facilities.values()
            if self.resource_service is not None
            and self.resource_service.resource_for_facility(facility.kind) == resource_id
            and agent_knows_facility(self.world, agent_id, facility.id)
            and self._facility_queue_has_capacity(facility.id, resource=True)
        ]
        if not facilities:
            return None, None, "no_facility"

        candidates: list[tuple[int, int, str, ZoneRoute]] = []
        for facility in facilities:
            route = self._route(agent.zone_id, facility.zone_id, agent_id)
            if route is None:
                continue
            in_transit = sum(
                1
                for other in self.world.agents.values()
                if other.activity is Activity.MOVING and other.target_id == facility.id
            )
            load = (
                len(facility.resource_queue_agent_ids)
                + len(facility.resource_active_agent_ids)
                + in_transit
            )
            candidates.append((load, route.travel_minutes, facility.id, route))
        if not candidates:
            return min(facility.id for facility in facilities), None, "no_route"
        selected = min(candidates, key=lambda item: item[:3])
        return selected[2], selected[3], None

    def _schedule_travel(
        self,
        *,
        agent_id: str,
        route: ZoneRoute,
        route_points: tuple[Any, ...] = (),
        purpose: str,
        facility_id: str | None,
        minute: int,
        causation_id: str | None = None,
        support_request_id: str | None = None,
        support_staff_id: str | None = None,
        validate_state: bool = True,
    ) -> bool:
        agent = self.world.agents[agent_id]
        if route.origin_zone_id != agent.zone_id:
            raise ValueError("travel route origin does not match Agent zone")
        if route.travel_minutes <= 0:
            return False
        due_minute = minute + route.travel_minutes
        if due_minute >= self.world.duration_minutes:
            return False
        zone_capacity = self.world.zones[route.destination_zone_id].capacity
        if zone_capacity is not None and route.origin_zone_id != route.destination_zone_id:
            occupancy = sum(
                1
                for other in self.world.agents.values()
                if other.zone_id == route.destination_zone_id
            )
            incoming = incoming_travelers(self.world, route.destination_zone_id)
            if occupancy + incoming >= zone_capacity:
                return False

        pending_event_ids = {event.event_id for event in self.world.event_queue.pending()}
        while True:
            sequence = self.world.allocate_event_sequence()
            event_id = f"agent-travel-complete-{sequence}"
            task_id = f"agent-travel-{sequence}"
            if (
                event_id not in pending_event_ids
                and not self.world.event_log.contains_id(event_id)
                and task_id not in self.world.active_tasks
            ):
                break

        target_id = (
            facility_id
            if purpose in ("toilet_request", "resource_request")
            else route.destination_zone_id
        )
        task = ActiveTask(
            id=task_id,
            kind=(
                "travel_to_toilet"
                if purpose == "toilet_request"
                else (
                    "travel_to_resource"
                    if purpose == "resource_request"
                    else (
                        "staff_support_travel"
                        if purpose == "staff_support_travel"
                        else "travel_return"
                    )
                )
            ),
            agent_id=agent_id,
            resource_id=None,
            start_minute=minute,
            due_minute=due_minute,
        )
        travel_payload = {
            "agent_id": agent_id,
            "task_id": task_id,
            "purpose": purpose,
            "destination_zone_id": route.destination_zone_id,
            "facility_id": facility_id,
            "route_zone_ids": list(route.zone_ids),
            "route_edge_ids": list(route.edge_ids),
            "travel_minutes": route.travel_minutes,
        }
        if purpose == "resource_request":
            # Preserve the pre-travel location for an arrival admission rejection record.
            travel_payload["origin_zone_id"] = route.origin_zone_id
        if purpose == "staff_support_travel":
            if not all(
                isinstance(value, str) and value for value in (support_request_id, support_staff_id)
            ):
                raise ValueError("staff support travel requires request and staff IDs")
            travel_payload["request_id"] = support_request_id
            travel_payload["staff_id"] = support_staff_id
        event = ScheduledEvent(
            event_id=event_id,
            kind=EventKind.COMPLETION,
            due_minute=due_minute,
            priority=0,
            sequence=sequence,
            event_type="agent_travel_complete",
            payload=travel_payload,
            source_id=task_id,
            causation_id=causation_id,
            provenance={
                "source_type": "assumption",
                "source_id": self.world.model_configs.get("layout_routing", {}).get(
                    "source_id", "ASM-ZONE-ROUTING-V1"
                ),
                "policy_version": "shortest-travel-time-v1",
            },
        )
        self.world.active_tasks[task.id] = task
        agent.activity = Activity.MOVING
        agent.target_id = target_id
        self.world.schedule_event(event)
        if MOVEMENT_STATE_KEY in self.world.model_state:
            start_movement(
                self.world,
                agent_id=agent_id,
                route=route,
                purpose=purpose,
                facility_id=facility_id,
                started_minute=minute,
                due_minute=due_minute,
                event_id=event_id,
                task_id=task_id,
                route_points=route_points,
                support_request_id=support_request_id,
                support_staff_id=support_staff_id,
            )
        if validate_state:
            self.world.validate()
        return True

    def _schedule_staff_support_travel(
        self,
        agent_id: str,
        route: ZoneRoute,
        request_id: str,
        staff_id: str,
        minute: int,
    ) -> bool:
        return self._schedule_travel(
            agent_id=agent_id,
            route=route,
            purpose="staff_support_travel",
            facility_id=None,
            minute=minute,
            causation_id=request_id,
            support_request_id=request_id,
            support_staff_id=staff_id,
            validate_state=False,
        )

    def _facility_queue_has_capacity(self, facility_id: str, *, resource: bool = False) -> bool:
        """Apply an explicit optional queue bound while leaving legacy queues unlimited."""
        config = self.world.model_configs.get("facility_queues")
        if config is None:
            return True
        if not isinstance(config, dict) or config.get("schema_version") != 1:
            raise ValueError("facility_queues config must use schema_version 1")
        capacities = config.get("queue_capacity_by_facility", {})
        if not isinstance(capacities, dict):
            raise ValueError("facility_queues queue_capacity_by_facility must be an object")
        maximum = capacities.get(facility_id)
        if maximum is None:
            return True
        if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 0:
            raise ValueError("facility queue capacity must be a non-negative integer")
        facility = self.world.facilities[facility_id]
        queued = facility.resource_queue_agent_ids if resource else facility.queue_agent_ids
        active = facility.resource_active_agent_ids if resource else facility.active_agent_ids
        transit = sum(
            1
            for agent in self.world.agents.values()
            if agent.activity is Activity.MOVING and agent.target_id == facility_id
        )
        return len(queued) + len(active) + transit < maximum

    def _physically_closed_edges(self, geometry_raw: Any, mobility: str) -> set[str]:
        """The surveyed routes that a person of this mobility cannot pass.

        They depend only on the geometry and constraint configs and the overlay's
        partitions and moved facilities, and are computed again when any of these differ
        from the copies kept with the answer. Every trip plan asks, for every route.
        """
        world = self.world
        overlay = world.model_state.get(GEOMETRY_OVERLAY_KEY)
        inputs = (
            geometry_raw,
            world.model_configs.get("spatial_constraints"),
            overlay is None,
            None if overlay is None else overlay.get("partitions"),
            None if overlay is None else overlay.get("facility_points"),
        )
        known = self._closed_edges.get(mobility)
        if known is not None and known[0] == inputs:
            return known[1]
        closed = {
            route.edge_id
            for route in parsed_facility_geometry(geometry_raw).routes
            if not route_is_physically_clear(world, route.edge_id, mobility=mobility)
        }
        self._closed_edges[mobility] = (deepcopy(inputs), closed)
        return closed

    def _route(
        self, origin_zone_id: str, destination_zone_id: str, agent_id: str
    ) -> ZoneRoute | None:
        """Route around full modeled passages and record capacity waits as audit events."""
        agent = self.world.agents.get(agent_id)
        space = self.space
        geometry_raw = self.world.model_configs.get("facility_geometry")
        if agent is not None and geometry_raw is not None:
            inaccessible = self._physically_closed_edges(geometry_raw, agent.profile.mobility)
            if inaccessible:
                graph = ZoneGraph(
                    zones=self.space.graph.zones,
                    edges=tuple(
                        edge for edge in self.space.graph.edges if edge.id not in inaccessible
                    ),
                )
                space = SpaceRouter(graph, self.space.policy)
        if not self.route_capacities:
            route = space.route(origin_zone_id, destination_zone_id, agent_id)
            return (
                route_with_movement_time(self.world, agent_id, route)
                if route is not None and agent_id in self.world.agents
                else route
            )
        loads = self._active_route_edge_loads()
        route = space.route(origin_zone_id, destination_zone_id, agent_id, edge_loads=loads)
        if route is None:
            unconstrained = space.route(
                origin_zone_id, destination_zone_id, agent_id, edge_loads={}
            )
            if unconstrained is not None:
                edge_by_id = {edge.id: edge for edge in self.space.graph.edges}
                for edge_id in unconstrained.edge_ids:
                    capacity = self.route_capacities.get(edge_id)
                    active = loads.get(edge_id, 0)
                    if capacity is not None and active >= capacity:
                        self._record_route_capacity_wait(
                            agent_id,
                            origin_zone_id,
                            destination_zone_id,
                            edge_id,
                            capacity,
                            active,
                        )
                        break
                    if edge_id not in edge_by_id:
                        break
        return (
            route_with_movement_time(self.world, agent_id, route)
            if route is not None and agent_id in self.world.agents
            else route
        )

    def _facility_approach(
        self, agent_id: str, facility_id: str
    ) -> tuple[ZoneRoute, tuple[Any, ...]] | None:
        """Plan the wall-aware, mobility-specific room path to the service point."""
        return facility_approach_route(self.world, agent_id, facility_id)

    def _active_route_edge_loads(self) -> dict[str, int]:
        loads: dict[str, int] = {}
        for event in self.world.event_queue.pending():
            if (
                event.event_type != "agent_travel_complete"
                or event.due_minute <= self.world.minute_index
            ):
                continue
            edge_ids = event.payload.get("route_edge_ids", [])
            if not isinstance(edge_ids, (list, tuple)):
                continue
            for edge_id in edge_ids:
                if isinstance(edge_id, str):
                    loads[edge_id] = loads.get(edge_id, 0) + 1
        operations = self.world.model_state.get("staff_operations_v1", {})
        if isinstance(operations, dict):
            for record in operations.get("staff", {}).values():
                if isinstance(record, dict) and record.get("phase") == "travel":
                    for edge_id in record.get("route_edge_ids", ()):
                        if isinstance(edge_id, str):
                            loads[edge_id] = loads.get(edge_id, 0) + 1
        return loads

    def _record_route_capacity_wait(
        self,
        agent_id: str,
        origin_zone_id: str,
        destination_zone_id: str,
        edge_id: str,
        capacity: int,
        active_travelers: int,
    ) -> None:
        sequence = self.world.allocate_event_sequence()
        source_id = "ASM-A05-ROUTE-EDIT-V1"
        event = ScheduledEvent(
            event_id=f"zone-edge-capacity-wait-{sequence}",
            kind=EventKind.ENDOGENOUS,
            due_minute=self.world.minute_index,
            priority=0,
            sequence=sequence,
            event_type="zone_edge_capacity_wait",
            payload={
                "agent_id": agent_id,
                "origin_zone_id": origin_zone_id,
                "destination_zone_id": destination_zone_id,
                "edge_id": edge_id,
                "capacity": capacity,
                "active_travelers": active_travelers,
            },
            source_id=source_id,
            provenance={"source_type": "assumption", "source_id": source_id},
        )
        self.world.event_log.append(
            EventLogRecord(
                event=event,
                realized_minute=self.world.minute_index,
                status=EventStatus.SUPPRESSED,
                detail="route edge capacity is full; the Agent remains at its origin",
            )
        )


def _agent_view_snapshot(agent: AgentView) -> dict[str, Any]:
    """Copy the pre-decision state into an inspectable, identifier-only record."""
    snapshot = {
        "agent_id": agent.id,
        "zone_id": agent.zone_id,
        "activity": agent.activity.value,
        "target_id": agent.target_id,
        "needs": {
            "toilet_urgency": agent.needs.toilet_urgency,
            "hunger": agent.needs.hunger,
            "fatigue": agent.needs.fatigue,
            "hygiene_need": agent.hygiene_need,
        },
        "psychological": {
            "sleep_debt_minutes": agent.sleep_debt_minutes,
            "uncertainty": agent.uncertainty,
            "reassurance": agent.reassurance,
            "discontent": agent.discontent,
            "positive_affect": agent.positive_affect,
            "negative_affect": agent.negative_affect,
        },
        "sanitation_level": agent.sanitation_level,
        "stress": agent.stress,
        "trust": agent.trust,
        "queue": {
            "task_id": agent.queue_task_id,
            "kind": agent.queue_kind,
            "target_id": agent.queue_target_id,
            "wait_minutes": agent.queue_wait_minutes,
        },
        "support": {
            "request_id": agent.support_request_id,
            "status": agent.support_request_status,
            "assigned_staff_id": agent.support_assigned_staff_id,
            "wait_minutes": agent.support_wait_minutes,
            "cooldown_remaining_minutes": agent.support_cooldown_remaining_minutes,
        },
    }
    if agent.resource_service_enabled:
        snapshot["needs"]["hydration_need"] = agent.needs.hydration_need
    if agent.zone_environment_modifier_event_ids or agent.demand_modifiers:
        snapshot["zone_environment"] = {
            "ambient_noise_level": agent.ambient_noise_level,
            "rest_efficiency_multiplier": agent.rest_efficiency_multiplier,
            "modifier_event_ids": list(agent.zone_environment_modifier_event_ids),
            "assumption_ids": list(agent.zone_environment_assumption_ids),
            "demand_modifiers": [
                {
                    "action": item.action,
                    "probability_multiplier": item.probability_multiplier,
                    "duration_multiplier": item.duration_multiplier,
                    "event_ids": list(item.modifier_event_ids),
                    "assumption_ids": list(item.assumption_ids),
                }
                for item in agent.demand_modifiers
            ],
        }
    return snapshot
