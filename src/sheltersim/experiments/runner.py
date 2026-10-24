"""Offline composition of the tick engine, Agent policies, travel, and facilities."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable

from sheltersim.contracts.extensions import FacilityGeometry
from sheltersim.contracts.policies import selected_models
from sheltersim.core.engine import TickEngine, TickPhase, TraceLevel
from sheltersim.core.rng import KeyedRandom
from sheltersim.core.world import WorldState
from sheltersim.domain.action_lifecycle import (
    active_action_index,
    finalize_active_actions,
    record_action_proposal,
    record_applied_action_completion,
    record_applied_population_exit,
)
from sheltersim.domain.agent_dynamics import (
    agent_knows_facility,
    learn_facility,
)
from sheltersim.domain.agent_dynamics import (
    record_proposal as record_agent_proposal,
)
from sheltersim.domain.care_receipts import (
    append_applied_care_transition,
    care_receipt_for_request,
    care_source_ids_for_agent,
    ingest_applied_shared_operation_records,
    make_staff_support_continuity_record,
    support_kind_for_agent,
)
from sheltersim.domain.distress_monitor import record_distress_monitor_tick
from sheltersim.domain.facilities import ToiletServiceManager
from sheltersim.domain.facility_queues import facility_has_independent_queues
from sheltersim.domain.geometry_overlay import release_sleep_bed, reserve_sleep_bed
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
from sheltersim.domain.infrastructure import INFRASTRUCTURE_EVENT_TYPES
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
    advance_movement,
    facility_approach_route,
    finish_movement,
    route_is_physically_clear,
    route_with_movement_time,
    start_movement,
)
from sheltersim.domain.population import admit_pending_arrivals
from sheltersim.domain.population_lifecycle import current_household_id
from sheltersim.domain.psychological_state import PSYCHOLOGICAL_STATE_KEY
from sheltersim.domain.resource_service import ResourceServiceManager
from sheltersim.domain.simulator_state import (
    initialize_simulator_extensions,
    validate_simulator_extensions,
)
from sheltersim.domain.social_dialogue import capture_social_state_context
from sheltersim.domain.social_network import (
    PROXIMITY_CONTACT_SOURCE_ID,
    canonical_proximity_contact_config,
    record_observed_contact,
)
from sheltersim.domain.staff import StaffService
from sheltersim.domain.staff_dynamics import update_staff_dynamics
from sheltersim.domain.staff_operations import (
    assign_staff_operations,
    interrupt_staff_trips_on_closed_routes,
    schedule_staff_operation_relief,
    update_staff_operations,
)
from sheltersim.domain.zones import ZoneGraph
from sheltersim.events.effects import (
    EffectProgramEnvironmentHandler,
    EffectProgramFacilityHandler,
    EffectProgramGeometryHandler,
    EffectProgramStructureHandler,
    EffectProgramTemporarySpaceHandler,
)
from sheltersim.events.handlers import (
    AgentTravelCompletionHandler,
    DeliveryDelayHandler,
    EffectProgramPopulationHandler,
    EventDispatcher,
    FacilityOutageHandler,
    FacilityRecoveryHandler,
    InformationContactHandler,
    InformationCorrectionHandler,
    InformationRumorHandler,
    InfrastructureEventHandler,
    ProximityContactHandler,
    ResourceLedgerHandler,
    ResourceServiceCancellationHandler,
    ResourceServiceCompletionHandler,
    SocialInteractionHandler,
    StaffOperationArrivalHandler,
    StaffOperationCompletionHandler,
    StaffOperationReliefHandler,
    StaffRestorationHandler,
    StaffSupportCompletionHandler,
    StaffUnavailabilityHandler,
    SupportRequestTimeoutHandler,
    ToiletServiceCompletionHandler,
    WaterOutageHandler,
    WaterRestorationHandler,
)
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.experiments.scenario import ScenarioSpec, ToiletServiceConfig
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.llm.contracts import LLMStage, LLMTask
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.report import (
    build_review_evidence,
    generate_evidence_review,
    render_review_markdown,
    scenario_assumption_ids,
    validate_comparison_scope,
)
from sheltersim.llm.runtime import (
    LEGACY_SUPPORT_PRIORITY_POLICY_VERSION,
    SUPPORT_PRIORITY_POLICY_VERSION,
    EventTriggeredSocialPolicy,
    SupportPriorityLimits,
    support_priority_policy_version,
    support_priority_runtime,
)
from sheltersim.llm.schemas import SUPPORT_ADVICE_SCHEMA
from sheltersim.policies.behavior import (
    ActionKind,
    AgentView,
    BehaviorDecisionRunner,
    WorldView,
    behavior_policy_definition,
)
from sheltersim.policies.proximity import (
    DisabledProximityPolicy,
    DistanceSampledProximityPolicy,
)
from sheltersim.policies.psychology import apply_state_deltas
from sheltersim.policies.registry import (
    create_behavior,
    create_information,
    create_installed_model,
    create_psychology,
    create_social,
)
from sheltersim.policies.social import (
    SOCIAL_RUNTIME_STATE_KEY,
)
from sheltersim.policies.space import SpaceRouter, ZoneRoute

if TYPE_CHECKING:
    from pathlib import Path


class OfflineSimulationController:
    """Connect the seven tick phases to deterministic, offline domain handlers."""

    def __init__(
        self,
        world: WorldState,
        artifacts: RunArtifactWriter,
        *,
        llm_router: LLMRouter | None = None,
    ) -> None:
        self.world = world
        self.active_actions = active_action_index(world)
        initialize_simulator_extensions(world)
        validate_simulator_extensions(world)
        proximity_model_id = selected_models(world.model_configs).get("social_proximity")
        if proximity_model_id is not None:
            self.proximity_policy = create_installed_model("social_proximity", world.model_configs)
        else:
            legacy_config = canonical_proximity_contact_config(world.model_configs.get("social_proximity"))
            self.proximity_policy = (
                DistanceSampledProximityPolicy(legacy_config)
                if legacy_config is not None
                else DisabledProximityPolicy()
            )
        self.proximity_contact_config = getattr(self.proximity_policy, "config", None)
        if self.proximity_contact_config is not None and (
            MOVEMENT_STATE_KEY not in world.model_state
            or "body_state_v1" not in world.model_state
            or "social_network_v1" not in world.model_state
        ):
            raise ValueError("social_proximity requires installed movement and social state")
        self.artifacts = artifacts
        self.llm_router = llm_router
        self.support_priority_version = (
            support_priority_policy_version(llm_router)
            if llm_router is not None
            else SUPPORT_PRIORITY_POLICY_VERSION
        )
        self.support_priority_limits = SupportPriorityLimits()
        self.social_policy = (
            EventTriggeredSocialPolicy(
                llm_router,
                record_decision=artifacts.record_social_decision,
            )
            if llm_router is not None
            else None
        )
        behavior_config = world.model_configs.get("behavior", {})
        interval = behavior_config.get("decision_interval_minutes", 15)
        if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
            raise ValueError("behavior decision_interval_minutes must be a positive integer")
        temperature = behavior_config.get("temperature", 0.2)
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
            raise ValueError("behavior temperature must be numeric")
        information_config = InformationSpreadConfig.from_mapping(
            world.model_configs.get("information_spread")
        )
        self.information_policy = create_information(world.model_configs)
        resource_service_enabled = world.schema_version == 6
        policy_variant = behavior_config.get("policy", "utility-softmax-v1")
        if not isinstance(policy_variant, str):
            raise ValueError("behavior policy must be a registered string variant")
        policy_parameters = {
            "temperature": temperature,
            "decision_interval_minutes": interval,
            "decision_interval_minutes_by_agent": behavior_config.get(
                "decision_interval_minutes_by_agent", {}
            ),
            "temperature_by_agent": behavior_config.get("temperature_by_agent", {}),
            "resource_service_enabled": resource_service_enabled,
            "autonomy_enabled": bool(
                world.model_configs.get("simulator_extensions", {}).get("enabled", False)
                or "population_context" in world.model_configs
                or "body_state_v1" in world.model_state
            ),
            "hydration_action_threshold": world.model_configs.get("resource_service", {}).get(
                "hydration_action_threshold", 0.25
            ),
            "hunger_action_threshold": world.model_configs.get("resource_service", {}).get(
                "hunger_action_threshold", 0.7
            ),
            "water_utility_base": world.model_configs.get("resource_service", {}).get(
                "water_utility_base", 0.18
            ),
            "water_utility_need_weight": world.model_configs.get("resource_service", {}).get(
                "water_utility_need_weight", 1.0
            ),
            "meal_utility_base": world.model_configs.get("resource_service", {}).get(
                "meal_utility_base", 0.18
            ),
            "meal_utility_need_weight": world.model_configs.get("resource_service", {}).get(
                "meal_utility_need_weight", 0.85
            ),
            "information_query_threshold": (
                information_config.query_threshold if information_config.enabled else 1.1
            ),
            "information_query_utility_weight": (
                information_config.query_utility_weight if information_config.enabled else 0.0
            ),
        }
        behavior_policy_definition(
            policy_variant,
            resource_service_enabled=resource_service_enabled,
            autonomy_enabled=policy_parameters["autonomy_enabled"],
        )
        self.decisions = BehaviorDecisionRunner(
            create_behavior(world.model_configs, **policy_parameters)
        )
        self.resource_service = ResourceServiceManager(world) if world.schema_version == 6 else None
        self.psychology = create_psychology(
            world.model_configs, resource_service_enabled=resource_service_enabled
        )
        self.stress_policy = create_social(world.model_configs)
        neighbor_sets: dict[str, set[str]] = {zone_id: set() for zone_id in world.zones}
        for edge in world.zone_edges.values():
            if edge.closed:
                continue
            neighbor_sets[edge.from_zone_id].add(edge.to_zone_id)
            neighbor_sets[edge.to_zone_id].add(edge.from_zone_id)
        self.zone_neighbors = {
            zone_id: tuple(sorted(neighbors)) for zone_id, neighbors in neighbor_sets.items()
        }
        service_config = ToiletServiceConfig.model_validate(
            world.model_configs.get("toilet_service", {})
        )
        self.toilet_service = ToiletServiceManager(
            world,
            min_service_minutes=service_config.minimum_minutes,
            max_service_minutes=service_config.maximum_minutes,
        )
        staff_config = world.model_configs.get("staff", {})
        self.staff_service = StaffService(
            world,
            task_duration_minutes=staff_config.get("support_task_minutes", 10),
            unresolved_after_minutes=staff_config.get("unresolved_after_minutes", 90),
            cooldown_minutes=staff_config.get("support_cooldown_minutes", 30),
            fairness_after_minutes=staff_config.get("support_fairness_minutes"),
            route_resolver=self._route,
            travel_scheduler=self._schedule_staff_support_travel,
        )
        route_config = world.model_configs.get("layout_routing", {})
        self.route_capacities = route_config.get("max_agents_in_transit_by_edge", {})
        self._refresh_space()
        infrastructure_handler = InfrastructureEventHandler()
        self.event_dispatcher = EventDispatcher(
            {
                "facility_outage": FacilityOutageHandler(),
                "facility_recovery": FacilityRecoveryHandler(),
                "information_rumor": InformationRumorHandler(),
                "information_correction": InformationCorrectionHandler(),
                "information_contact_update": InformationContactHandler(),
                "proximity_contact_update": ProximityContactHandler(),
                "effect_program_apply": EffectProgramFacilityHandler(),
                "effect_program_expire": EffectProgramFacilityHandler(expire=True),
                "effect_program_structure": EffectProgramStructureHandler(),
                "effect_program_temporary_space": EffectProgramTemporarySpaceHandler(),
                "effect_program_temporary_space_expire": EffectProgramTemporarySpaceHandler(expire=True),
                "effect_program_population": EffectProgramPopulationHandler(),
                "population_approach_complete": EffectProgramPopulationHandler(),
                "effect_program_geometry": EffectProgramGeometryHandler(),
                "effect_program_geometry_expire": EffectProgramGeometryHandler(expire=True),
                "effect_program_environment": EffectProgramEnvironmentHandler(),
                "effect_program_environment_expire": EffectProgramEnvironmentHandler(expire=True),
                "social_interaction": SocialInteractionHandler(),
                "toilet_service_complete": ToiletServiceCompletionHandler(
                    after_complete=self._schedule_return_to_initial_zone
                ),
                "agent_travel_complete": AgentTravelCompletionHandler(
                    self.toilet_service,
                    resource_service=self.resource_service,
                    validate_request_state=False,
                ),
                "staff_support_complete": StaffSupportCompletionHandler(
                    cooldown_minutes=self.staff_service.cooldown_minutes,
                    require_colocated=True,
                ),
                "staff_operation_arrive": StaffOperationArrivalHandler(),
                "staff_operation_relief": StaffOperationReliefHandler(),
                "staff_operation_complete": StaffOperationCompletionHandler(),
                "support_request_timeout": SupportRequestTimeoutHandler(
                    cooldown_minutes=self.staff_service.cooldown_minutes
                ),
                "resource_delivery": ResourceLedgerHandler("delivery"),
                "delivery_delayed": DeliveryDelayHandler(),
                "water_outage": WaterOutageHandler(),
                "water_restored": WaterRestorationHandler(),
                "staff_unavailable": StaffUnavailabilityHandler(),
                "staff_shift_change": StaffUnavailabilityHandler(),
                "staff_restored": StaffRestorationHandler(),
                **{
                    event_type: infrastructure_handler
                    for event_type in INFRASTRUCTURE_EVENT_TYPES
                    if event_type != "staff_shift_change"
                },
                "resource_loss": ResourceLedgerHandler("loss"),
                "resource_reserve": ResourceLedgerHandler("reserve"),
                "resource_release": ResourceLedgerHandler("release"),
                "resource_consume": ResourceLedgerHandler("consume"),
                **(
                    {
                        "resource_service_complete": ResourceServiceCompletionHandler(
                            self.resource_service
                        ),
                        "resource_service_cancel": ResourceServiceCancellationHandler(
                            self.resource_service
                        ),
                    }
                    if self.resource_service is not None
                    else {}
                ),
            }
        )

    def _refresh_space(self) -> None:
        """Rebuild immutable routing snapshots after an applied graph/geometry change."""
        world = self.world
        self.space = SpaceRouter(
            ZoneGraph.from_states(
                world.zones,
                world.zone_edges,
                edge_capacities=self.route_capacities,
            )
        )
        staff_graph = self.space.graph
        if "facility_geometry" in world.model_configs:
            staff_graph = ZoneGraph(
                zones=staff_graph.zones,
                edges=tuple(
                    edge
                    for edge in staff_graph.edges
                    if route_is_physically_clear(world, edge.id, mobility="independent")
                ),
            )
        self.staff_space = SpaceRouter(staff_graph, self.space.policy)

    def __call__(self, phase: TickPhase, world: WorldState, minute: int, rng: KeyedRandom) -> None:
        if world is not self.world:
            raise ValueError("offline simulation controller is bound to a different WorldState")
        if phase is TickPhase.DISPATCH_DUE_EVENTS:
            outcomes = self.event_dispatcher.dispatch_due(
                world,
                minute,
                after_event=lambda records: ingest_applied_shared_operation_records(
                    world, records
                ),
            )
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
                            request_id = outcome.event.payload.get("request_id")
                            request = world.support_requests.get(request_id)
                            prior = care_receipt_for_request(world, request_id)
                            if (
                                request is not None
                                and prior is not None
                                and request.status is SupportRequestStatus.ASSIGNED
                                and request.assigned_staff_id == outcome.event.payload.get("staff_id")
                                and world.agents[request.agent_id].activity is Activity.SEEKING_HELP
                            ):
                                append_applied_care_transition(
                                    world,
                                    request_id=request.id,
                                    agent_id=request.agent_id,
                                    household_id=current_household_id(world, request.agent_id),
                                    support_kind=prior["support_kind"],
                                    status="started",
                                    staff_id=request.assigned_staff_id,
                                    causation_id=request.task_id,
                                    source_ids=care_source_ids_for_agent(world, request.agent_id),
                                )
                    if outcome.event.event_type == "support_request_timeout":
                        request_id = outcome.event.payload.get("request_id")
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
                                household_id=current_household_id(world, request.agent_id),
                                support_kind=prior["support_kind"],
                                status="interrupted",
                                causation_id=outcome.event.event_id,
                                source_ids=care_source_ids_for_agent(world, request.agent_id),
                            )
            geometry_changed = any(
                outcome.status is EventStatus.APPLIED
                and outcome.event.event_type in {
                    "effect_program_geometry", "effect_program_geometry_expire",
                    "effect_program_structure",
                }
                for outcome in outcomes
            )
            if geometry_changed:
                self._refresh_space()
            for outcome in outcomes:
                event = outcome.event
                parameters = event.payload.get("parameters", {})
                if (
                    outcome.status is EventStatus.APPLIED
                    and event.event_type == "effect_program_geometry"
                    and event.payload.get("op") == "set_route_access"
                    and isinstance(parameters, dict)
                    and parameters.get("accessible") is False
                ):
                    interrupt_staff_trips_on_closed_routes(world, event)
            from sheltersim.domain.population_lifecycle import admit_pending_returns
            admit_pending_returns(world, minute)
            self._sync_movement_after_events()
            admit_pending_arrivals(world, minute)
            self._consider_social_contact_events(outcomes)
        elif phase is TickPhase.UPDATE_AGENT_STATE:
            self._update_movement(minute)
            view = WorldView.from_world(world, include_state_hash=False)
            psychology_result = self.psychology.explain_update(view, minute)
            base_deltas = psychology_result.deltas
            if self.stress_policy.parameters.enabled:
                result = self.stress_policy.update(
                    view,
                    base_deltas,
                    rng,
                    zone_neighbors=self.zone_neighbors,
                    runtime_state=world.model_state.get(SOCIAL_RUNTIME_STATE_KEY),
                )
                final_deltas = result.deltas
                world.model_state[SOCIAL_RUNTIME_STATE_KEY] = result.runtime_state
                for contact in result.contacts if "social_network_v1" in world.model_state else ():
                    record_observed_contact(
                        world,
                        recipient_id=contact.recipient_id,
                        source_id=contact.source_id,
                        minute=minute,
                    )
                if self.artifacts.trace_level is not TraceLevel.OFF:
                    self.artifacts.record_social_contact_trace(result.trace_document(minute))
            else:
                final_deltas = base_deltas
            if self.artifacts.trace_level is not TraceLevel.OFF:
                final_by_agent = {delta.agent_id: delta for delta in final_deltas}
                base_by_agent = {delta.agent_id: delta for delta in base_deltas}
                traced_result = replace(
                    psychology_result,
                    traces=tuple(
                        replace(
                            trace,
                            stress_after=final_by_agent[trace.agent_id].stress,
                            social_stress_delta=(
                                final_by_agent[trace.agent_id].stress
                                - base_by_agent[trace.agent_id].stress
                            ),
                        )
                        for trace in psychology_result.traces
                    ),
                )
                self.artifacts.record_psychology_trace(
                    traced_result.trace_document(
                        minute,
                        source_id=self.psychology.source_id,
                        full=self.artifacts.trace_level is TraceLevel.FULL,
                    ),
                    agent_contributions=(
                        {
                            "agent_id": trace.agent_id,
                            "stress_before": trace.stress_before,
                            "stress_after": final_by_agent[trace.agent_id].stress,
                            "raw_stress_delta": trace.raw_stress_delta,
                            "social_stress_delta": (
                                final_by_agent[trace.agent_id].stress
                                - base_by_agent[trace.agent_id].stress
                            ),
                            "component_contributions": dict(trace.component_contributions),
                        }
                        for trace in psychology_result.traces
                    ),
                )
            update_long_term = getattr(self.psychology, "update_long_term_state", None)
            if callable(update_long_term):
                record_distress_monitor_tick(
                    world.metric_state,
                    previous_by_agent={
                        agent_id: agent.stress for agent_id, agent in view.agents.items()
                    },
                    current_by_agent={
                        delta.agent_id: delta.stress for delta in final_deltas
                    },
                    threshold=self.psychology.parameters.high_distress_threshold,
                )
                runtime_state = world.model_state.get(PSYCHOLOGICAL_STATE_KEY)
                if not isinstance(runtime_state, dict):
                    raise ValueError("mean-reverting psychology runtime state is missing")
                world.model_state[PSYCHOLOGICAL_STATE_KEY] = update_long_term(
                    runtime_state, minute, final_deltas
                )
            apply_state_deltas(world, final_deltas)
        elif phase is TickPhase.PROPOSE_ACTIONS:
            view = WorldView.from_world(world)
            for proposal in self.decisions.propose_due(view, rng):
                self._apply_proposal(
                    proposal,
                    observed_state=_agent_view_snapshot(view.agents[proposal.agent_id]),
                )
        elif phase is TickPhase.ARBITRATE_ACTIONS:
            update_staff_dynamics(world, minute)
            update_staff_operations(world, minute)
            self.toilet_service.arbitrate(minute, rng, validate_state=False)
            if self.resource_service is not None:
                self.resource_service.arbitrate(minute, rng, validate_state=False)
            assigned_requests = self.staff_service.assign_pending(minute, validate_state=False)
            for request_id in assigned_requests:
                prior = care_receipt_for_request(world, request_id)
                request = world.support_requests.get(request_id)
                if prior is None or request is None or request.assigned_staff_id is None:
                    continue
                append_applied_care_transition(
                    world,
                    request_id=request.id,
                    agent_id=request.agent_id,
                    household_id=current_household_id(world, request.agent_id),
                    support_kind=prior["support_kind"],
                    status="assigned",
                    staff_id=request.assigned_staff_id,
                    causation_id=request.task_id,
                    source_ids=care_source_ids_for_agent(world, request.agent_id),
                )
                if world.agents[request.agent_id].activity is Activity.SEEKING_HELP:
                    append_applied_care_transition(
                        world,
                        request_id=request.id,
                        agent_id=request.agent_id,
                        household_id=current_household_id(world, request.agent_id),
                        support_kind=prior["support_kind"],
                        status="started",
                        staff_id=request.assigned_staff_id,
                        causation_id=request.task_id,
                        source_ids=care_source_ids_for_agent(world, request.agent_id),
                    )
            # Reserve eligible staff for direct occupant support before background patrol,
            # outreach, sanitation and maintenance work consumes the remaining staff.
            assign_staff_operations(world, minute, self.staff_space)
            self._consider_due_complaints(minute)
            schedule_staff_operation_relief(world, minute)
        elif phase is TickPhase.SAMPLE_ENDOGENOUS_EVENTS:
            self._sample_information_contacts(minute, rng)
            self._sample_proximity_contacts(minute)
        elif phase is TickPhase.COLLECT_METRICS:
            self.artifacts.capture_metric(world, minute=minute)
            if minute + 1 >= world.duration_minutes:
                finalize_active_actions(
                    world, minute, active_actions=self.active_actions
                )

    def _sample_information_contacts(self, minute: int, rng: KeyedRandom) -> None:
        if self.information_policy is None:
            return
        result = self.information_policy.update(
            WorldView.from_world(self.world),
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

    def _consider_due_complaints(self, minute: int) -> None:
        """Evaluate one request exactly when it reaches the modeled wait threshold."""
        if self.social_policy is None:
            return
        threshold = self.social_policy.limits.complaint_wait_minutes
        for request_id, request in sorted(self.world.support_requests.items()):
            if (
                request.status is SupportRequestStatus.PENDING
                and request.requested_minute + threshold == minute
            ):
                self.social_policy.consider_complaint_response(
                    self.world,
                    agent_id=request.agent_id,
                    request_id=request_id,
                )

    def _consider_social_contact_events(self, outcomes: tuple[Any, ...]) -> None:
        """Use only successfully applied local contacts as social interaction triggers."""
        if self.social_policy is None:
            return
        for outcome in outcomes:
            event = outcome.event
            if (
                outcome.status is not EventStatus.APPLIED
                or event.event_type != "information_contact_update"
            ):
                continue
            sender_id = event.payload.get("sender_agent_id")
            recipient_id = event.payload.get("recipient_agent_id")
            message_id = event.payload.get("message_id")
            if not all(isinstance(value, str) for value in (sender_id, recipient_id)):
                continue
            sender = self.world.agents.get(sender_id)
            recipient = self.world.agents.get(recipient_id)
            if sender is None or recipient is None or sender_id == recipient_id:
                continue
            if (
                current_household_id(self.world, sender.id) is not None
                and current_household_id(self.world, sender.id) == current_household_id(self.world, recipient.id)
                and max(sender.stress, recipient.stress)
                >= self.social_policy.limits.stress_threshold
            ):
                self.social_policy.consider_cooperation(
                    self.world,
                    initiator_agent_id=sender_id,
                    participant_agent_id=recipient_id,
                    trigger_event_id=event.event_id,
                )
            state = self.world.model_state.get(INFORMATION_SPREAD_STATE_KEY)
            if not isinstance(message_id, str) or not isinstance(state, dict):
                continue
            raw_message = state["messages"].get(message_id)
            raw_belief = state["beliefs"].get(recipient_id, {}).get(message_id)
            if raw_message is None or raw_belief is None:
                continue
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
                        position.update({
                            "zone_id": agent.zone_id,
                            "point": reservation["point"].model_dump(mode="json"),
                            "edge_id": None,
                            "progress": 1.0,
                            "position_kind": "physical",
                        })
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
                if (
                    self.information_policy is not None
                    and (
                        proposal.action is ActionKind.SEEK_INFORMATION
                        or self.world.model_state[INFORMATION_SPREAD_STATE_KEY]["query_pressure"][agent.id]
                        >= self.information_policy.config.query_threshold
                    )
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
                ActionKind.SOCIALIZE: ("reassurance_exchange", "offer_reassurance", "emotional_check_in"),
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
                                    agent_id=agent.id, action=action, outcome="rejected",
                                    reason="request_rejected", facility_id=facility_id,
                                    origin_zone_id=agent.zone_id, detail=str(exc),
                                )
                                self.world.metric_state["resource_service_request_rejected_count"] = (
                                    self.world.metric_state.get("resource_service_request_rejected_count", 0.0) + 1.0
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
                                        self.world.metric_state["resource_service_request_rejected_count"] = (
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

        recorded_proposal = replace(
            proposal,
            target_id=target_id,
            next_decision_at=agent.next_decision_at,
        )
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
            ) and not (
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
            incoming = sum(
                1
                for other in self.world.agents.values()
                if other.activity is Activity.MOVING
                and self._travel_destination_zone(other.id) == route.destination_zone_id
            )
            if occupancy + incoming >= zone_capacity:
                return False

        pending_event_ids = {
            event.event_id for event in self.world.event_queue.pending()
        }
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
                isinstance(value, str) and value
                for value in (support_request_id, support_staff_id)
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
        queued = (
            facility.resource_queue_agent_ids if resource else facility.queue_agent_ids
        )
        active = (
            facility.resource_active_agent_ids if resource else facility.active_agent_ids
        )
        transit = sum(
            1
            for agent in self.world.agents.values()
            if agent.activity is Activity.MOVING and agent.target_id == facility_id
        )
        return len(queued) + len(active) + transit < maximum

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
                and journey.get("purpose") in {
                    "toilet_queue_position",
                    "resource_queue_position",
                    "toilet_queue_service",
                    "resource_queue_service",
                }
            )
            if agent is None or (
                agent.activity is not Activity.MOVING and not queue_trip_is_active
            ):
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
                    state["positions"][agent_id].update({
                        "zone_id": agent.zone_id,
                        "point": bed_point,
                        "edge_id": None,
                        "progress": 1.0,
                        "position_kind": "physical",
                    })

    def _update_movement(self, minute: int) -> None:
        """Advance route interpolation and atomically reschedule changed paths."""
        state = self.world.model_state.get(MOVEMENT_STATE_KEY)
        if state is None:
            return
        for agent_id in advance_movement(self.world, minute):
            journey = dict(state["journeys"].get(agent_id, {}))
            agent = self.world.agents.get(agent_id)
            if not journey or agent is None:
                continue
            old_event = next(
                (
                    event
                    for event in self.world.event_queue.pending()
                    if event.event_id == journey["event_id"]
                ),
                None,
            )
            if old_event is not None:
                self.world.event_queue.remove(old_event.event_id)
                self.world.event_log.append(
                    EventLogRecord(
                        event=old_event,
                        realized_minute=minute,
                        status=EventStatus.SUPPRESSED,
                        detail="the route changed before travel completion",
                    )
                )
            self.world.active_tasks.pop(journey["task_id"], None)
            old_position = state["positions"].get(agent_id, {})
            agent.zone_id = old_position.get("zone_id", agent.zone_id)
            agent.activity = Activity.IDLE
            agent.target_id = None
            # Keep the last applied physical point while planning the replacement path.
            # For a same-zone facility move, the new in-room route therefore begins
            # at the stopped actor position instead of a zone-centre placeholder.
            state["journeys"].pop(agent_id, None)
            target_zone = journey["destination_zone_id"]
            facility_id = journey.get("facility_id")
            if journey.get("purpose") == "staff_support_travel":
                if not self._replan_staff_support_trip(agent_id, journey, minute):
                    self.world.metric_state["movement_replan_blocked_count"] = (
                        self.world.metric_state.get("movement_replan_blocked_count", 0.0) + 1.0
                    )
                self.world.metric_state["movement_replan_count"] = (
                    self.world.metric_state.get("movement_replan_count", 0.0) + 1.0
                )
                continue
            if journey.get("purpose") == "sleep_bed":
                reservation = reserve_sleep_bed(self.world, agent_id)
                if reservation is not None and reservation["travel_minutes"] > 0:
                    route = ZoneRoute(
                        origin_zone_id=agent.zone_id,
                        destination_zone_id=agent.zone_id,
                        zone_ids=(agent.zone_id, agent.zone_id),
                        edge_ids=(),
                        travel_minutes=reservation["travel_minutes"],
                    )
                    scheduled = self._schedule_travel(
                        agent_id=agent.id,
                        route=route,
                        route_points=reservation["route_points"],
                        purpose="sleep_bed",
                        facility_id=None,
                        minute=minute,
                        causation_id=journey["event_id"],
                        validate_state=False,
                    )
                    if scheduled:
                        self.world.metric_state["movement_replan_count"] = (
                            self.world.metric_state.get("movement_replan_count", 0.0) + 1.0
                        )
                        continue
                if reservation is None or reservation["travel_minutes"] > 0:
                    self.world.metric_state["movement_replan_blocked_count"] = (
                        self.world.metric_state.get("movement_replan_blocked_count", 0.0) + 1.0
                    )
                    release_sleep_bed(self.world, agent_id)
                else:
                    state["positions"][agent_id].update({
                        "zone_id": agent.zone_id,
                        "point": reservation["point"].model_dump(mode="json"),
                        "edge_id": None,
                        "progress": 1.0,
                        "position_kind": "physical",
                    })
                agent.activity = Activity.SLEEPING
                agent.target_id = None
                self.world.metric_state["movement_replan_count"] = (
                    self.world.metric_state.get("movement_replan_count", 0.0) + 1.0
                )
                continue
            if facility_id in self.world.facilities:
                target_zone = self.world.facilities[facility_id].zone_id
                if agent.zone_id == target_zone:
                    room_route = self._facility_approach(agent_id, facility_id)
                    if room_route is not None and room_route[1]:
                        if room_route[0].travel_minutes > 0 and len(room_route[1]) > 1:
                            self._schedule_travel(
                                agent_id=agent_id,
                                route=room_route[0],
                                route_points=room_route[1],
                                purpose=journey["purpose"],
                                facility_id=facility_id,
                                minute=minute,
                                causation_id=journey["event_id"],
                                validate_state=False,
                            )
                        else:
                            state["positions"][agent_id] = {
                                "zone_id": agent.zone_id,
                                "point": room_route[1][-1].model_dump(mode="json"),
                                "edge_id": None,
                                "progress": 0.0,
                                "position_kind": "physical",
                            }
                    else:
                        self.world.metric_state["movement_replan_blocked_count"] = (
                            self.world.metric_state.get("movement_replan_blocked_count", 0.0) + 1.0
                        )
                    self.world.metric_state["movement_replan_count"] = (
                        self.world.metric_state.get("movement_replan_count", 0.0) + 1.0
                    )
                    continue
            route = self._route(agent.zone_id, target_zone, agent_id)
            if route is not None and route.edge_ids:
                self._schedule_travel(
                    agent_id=agent_id,
                    route=route,
                    purpose=journey["purpose"],
                    facility_id=facility_id,
                    minute=minute,
                    causation_id=journey["event_id"],
                    validate_state=False,
                )
            else:
                self.world.metric_state["movement_replan_blocked_count"] = (
                    self.world.metric_state.get("movement_replan_blocked_count", 0.0) + 1.0
                )
            self.world.metric_state["movement_replan_count"] = (
                self.world.metric_state.get("movement_replan_count", 0.0) + 1.0
            )

    def _replan_staff_support_trip(
        self, agent_id: str, journey: dict[str, Any], minute: int
    ) -> bool:
        """Re-route an assigned resident or requeue support if its route closed."""
        world = self.world
        request_id = journey.get("support_request_id")
        request = world.support_requests.get(request_id) if isinstance(request_id, str) else None
        agent = world.agents.get(agent_id)
        staff = world.staff.get(request.assigned_staff_id or "") if request is not None else None
        task = world.active_tasks.get(request.task_id or "") if request is not None else None
        if (
            request is None
            or request.status is not SupportRequestStatus.ASSIGNED
            or agent is None
            or staff is None
            or task is None
            or task.kind != "staff_support"
        ):
            raise ValueError("replanned resident trip lost its assigned support task")
        completions = [
            event for event in world.event_queue.pending()
            if event.event_type == "staff_support_complete"
            and event.payload.get("task_id") == task.id
        ]
        if len(completions) != 1 or completions[0].due_minute != task.due_minute:
            raise ValueError("replanned resident trip has inconsistent support completion")
        previous_completion = completions[0]
        service_minutes = task.due_minute - task.start_minute
        route = self._route(agent.zone_id, staff.zone_id, agent.id)
        due_minute = minute + (route.travel_minutes if route is not None else 0) + service_minutes
        can_schedule = route is not None and due_minute < world.duration_minutes
        if can_schedule and route is not None and route.travel_minutes > 0:
            can_schedule = self._schedule_travel(
                agent_id=agent.id,
                route=route,
                purpose="staff_support_travel",
                facility_id=None,
                minute=minute,
                causation_id=journey.get("event_id"),
                support_request_id=request.id,
                support_staff_id=staff.id,
                validate_state=False,
            )
        elif can_schedule:
            agent.activity = Activity.SEEKING_HELP
            agent.target_id = None
        if can_schedule:
            world.event_queue.remove(previous_completion.event_id)
            world.event_log.append(EventLogRecord(
                event=previous_completion,
                realized_minute=minute,
                status=EventStatus.SUPPRESSED,
                detail="support completion was rescheduled after a route change",
            ))
            sequence = world.allocate_event_sequence()
            task.start_minute = minute + (route.travel_minutes if route is not None else 0)
            task.due_minute = due_minute
            staff.busy_until = due_minute
            world.schedule_event(ScheduledEvent(
                event_id=f"staff-support-complete-{sequence}",
                kind=EventKind.COMPLETION,
                due_minute=due_minute,
                priority=0,
                sequence=sequence,
                event_type="staff_support_complete",
                payload={
                    "request_id": request.id,
                    "agent_id": agent.id,
                    "staff_id": staff.id,
                    "task_id": task.id,
                    "support_duration_minutes": service_minutes,
                },
                source_id=task.id,
                causation_id=journey.get("event_id"),
                provenance={
                    "source_type": "assumption",
                    "source_id": "ASM-STAFF-SUPPORT-LIFECYCLE-V1",
                    "policy_version": self.staff_service.policy_version,
                    "rescheduled_after_route_change": True,
                },
            ))
            return True

        world.event_queue.remove(previous_completion.event_id)
        world.event_log.append(EventLogRecord(
            event=previous_completion,
            realized_minute=minute,
            status=EventStatus.SUPPRESSED,
            detail="support completion was cancelled because the staff route is unavailable",
        ))
        del world.active_tasks[task.id]
        staff.work_queue.clear()
        staff.active_task_id = None
        staff.busy_until = 0
        request.assigned_staff_id = None
        request.task_id = None
        agent.target_id = None
        if request.timeout_minute <= minute:
            request.status = SupportRequestStatus.UNRESOLVED
            request.resolved_minute = minute
            request.cooldown_until = minute + self.staff_service.cooldown_minutes
            agent.activity = Activity.IDLE
            agent.support_request_id = None
            agent.support_cooldown_until = request.cooldown_until
            world.metric_state["support_unresolved_count"] = (
                world.metric_state.get("support_unresolved_count", 0.0) + 1.0
            )
            continuity_status = "unresolved"
        else:
            request.status = SupportRequestStatus.PENDING
            agent.activity = Activity.SEEKING_HELP
            agent.support_request_id = request.id
            continuity_status = "requeued"
        continuity = make_staff_support_continuity_record(
            world,
            request_id=request.id,
            agent_id=agent.id,
            task_id=task.id,
            previous_staff_id=staff.id,
            status=continuity_status,
            causation_id=journey.get("event_id") or task.id,
            cause_type="route_unreachable",
        )
        world.event_log.append(continuity)
        ingest_applied_shared_operation_records(world, (continuity,))
        finish_movement(world, agent.id)
        return False

    def _travel_destination_zone(self, agent_id: str) -> str | None:
        agent = self.world.agents[agent_id]
        if agent.target_id in self.world.zones:
            return agent.target_id
        if agent.target_id in self.world.facilities:
            return self.world.facilities[agent.target_id].zone_id
        return None

    def _route(
        self, origin_zone_id: str, destination_zone_id: str, agent_id: str
    ) -> ZoneRoute | None:
        """Route around full modeled passages and record capacity waits as audit events."""
        agent = self.world.agents.get(agent_id)
        space = self.space
        geometry_raw = self.world.model_configs.get("facility_geometry")
        if agent is not None and geometry_raw is not None:
            geometry = FacilityGeometry.model_validate(geometry_raw)
            inaccessible = {
                route.edge_id
                for route in geometry.routes
                if not route_is_physically_clear(
                    self.world, route.edge_id, mobility=agent.profile.mobility
                )
            }
            if inaccessible:
                graph = ZoneGraph(
                    zones=self.space.graph.zones,
                    edges=tuple(
                        edge
                        for edge in self.space.graph.edges
                        if edge.id not in inaccessible
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


def run_scenario(
    scenario: ScenarioSpec,
    output_directory: str | Path,
    *,
    trace_level: TraceLevel = TraceLevel.SUMMARY,
    llm_router: LLMRouter | None = None,
    comparison_report: dict[str, Any] | None = None,
    checkpoint_at_minute: int | None = None,
    checkpoint_callback: Callable[[WorldState, Any, LLMRouter | None], None] | None = None,
) -> WorldState:
    """Run one scenario; API routing is opt-in at this library boundary and automatic in CLI.

    An optional checkpoint callback runs once at an exact completed-minute boundary. It is
    intended for external durable checkpointing and does not alter engine state or ordering.
    """
    validate_comparison_scope(scenario, comparison_report)
    if (checkpoint_at_minute is None) != (checkpoint_callback is None):
        raise ValueError("checkpoint minute and callback must be provided together")
    if checkpoint_at_minute is not None and not 0 < checkpoint_at_minute < scenario.duration_minutes:
        raise ValueError("checkpoint minute must be inside the scenario duration")
    world = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(world)
    artifacts = RunArtifactWriter(scenario, output_directory, trace_level=trace_level)
    execution_mode = llm_router.mode.value if llm_router is not None else "offline"
    artifacts.write_initial(world, execution_mode=execution_mode)
    if llm_router is not None:
        llm_router.observer = artifacts.record_llm_call
    controller = OfflineSimulationController(world, artifacts, llm_router=llm_router)
    engine = TickEngine(world, phase_handler=controller)
    if checkpoint_callback is None:
        engine.run()
    else:
        assert checkpoint_at_minute is not None
        while world.minute_index < world.duration_minutes:
            engine.step()
            if world.minute_index == checkpoint_at_minute:
                checkpoint_callback(world, artifacts.metrics, llm_router)
    if llm_router is not None and llm_router.mode.value != "offline":
        review_evidence = build_review_evidence(
            metrics=[snapshot.csv_row() for snapshot in artifacts.metrics.snapshots],
            realized_events=[record.to_dict() for record in world.event_log.records],
            assumptions=scenario_assumption_ids(scenario),
            comparison_report=comparison_report,
        )
        review_result = generate_evidence_review(review_evidence, llm_router)
        artifacts.write_llm_review(
            render_review_markdown(review_result), structured=review_result.to_dict()
        )
    artifacts.finalize(world)
    return world


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

