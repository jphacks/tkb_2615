"""Offline composition of the tick engine, Agent policies, travel, and facilities."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Callable

from sheltersim.contracts.policies import selected_models, selected_models_scope
from sheltersim.core.engine import TickEngine, TickPhase, TraceLevel
from sheltersim.core.rng import KeyedRandom
from sheltersim.core.state_mirror import field_stamp, same_stamp
from sheltersim.core.world import WorldState
from sheltersim.domain.action_lifecycle import active_action_store
from sheltersim.domain.facilities import ToiletServiceManager
from sheltersim.domain.geometry_overlay import geometry_config_scope
from sheltersim.domain.infrastructure import INFRASTRUCTURE_EVENT_TYPES
from sheltersim.domain.models import InformationSpreadConfig
from sheltersim.domain.movement import (
    MOVEMENT_STATE_KEY,
    room_route_scope,
    route_is_physically_clear,
)
from sheltersim.domain.resource_service import ResourceServiceManager
from sheltersim.domain.simulator_state import (
    initialize_simulator_extensions,
    validate_simulator_extensions,
)
from sheltersim.domain.social_network import canonical_proximity_contact_config
from sheltersim.domain.staff import StaffService
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
from sheltersim.experiments.phases.arbitrate import ArbitratePhase
from sheltersim.experiments.phases.dispatch import DispatchPhase
from sheltersim.experiments.phases.metrics import MetricsPhase
from sheltersim.experiments.phases.propose import ProposePhase
from sheltersim.experiments.phases.sample import SamplePhase
from sheltersim.experiments.phases.segments import install_segments
from sheltersim.experiments.phases.update import UpdatePhase
from sheltersim.experiments.scenario import ScenarioSpec, ToiletServiceConfig
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.report import (
    build_review_evidence,
    generate_evidence_review,
    render_review_markdown,
    scenario_assumption_ids,
    validate_comparison_scope,
)
from sheltersim.llm.runtime import (
    SUPPORT_PRIORITY_POLICY_VERSION,
    EventTriggeredSocialPolicy,
    SupportPriorityLimits,
    support_priority_policy_version,
)
from sheltersim.policies.behavior import BehaviorDecisionRunner, behavior_policy_definition
from sheltersim.policies.proximity import DisabledProximityPolicy, DistanceSampledProximityPolicy
from sheltersim.policies.registry import (
    create_behavior,
    create_information,
    create_installed_model,
    create_psychology,
    create_social,
)
from sheltersim.policies.space import SpaceRouter
from sheltersim.policies.world_view import WorldView

if TYPE_CHECKING:
    from pathlib import Path


class OfflineSimulationController(
    DispatchPhase, UpdatePhase, ProposePhase, ArbitratePhase, SamplePhase, MetricsPhase
):
    """Connect the seven tick phases to deterministic, offline domain handlers."""

    def __init__(
        self,
        world: WorldState,
        artifacts: RunArtifactWriter,
        *,
        llm_router: LLMRouter | None = None,
    ) -> None:
        self.world = world
        self._person_contexts: dict[str, Any] = {}
        self._closed_edges: dict[str, tuple[Any, set[str]]] = {}
        self.active_actions = active_action_store(world)
        # The update phase's view, from which the next phase's view is made.
        self._view_before_proposals: WorldView | None = None
        # What rules/world_view.rs keeps between views.
        self._rust_view_cache: Any = None
        # What the phases parsed from model_configs, while its values stay the same.
        self._config_scopes_minute: int | None = None
        self._config_scopes_object: dict[str, Any] | None = None
        self._config_scopes_configs: dict[str, Any] | None = None
        self._config_scopes_stamp: tuple[Any, int] | None = None
        self._parsed_configs: dict[tuple[str, int], tuple[Any, Any]] = {}
        self._model_selections: list[tuple[Any, dict[str, str]]] = []
        self._room_routes: dict[str, Any] = {"routes": {}}
        self._zone_environments: dict[str, Any] = {}
        initialize_simulator_extensions(world)
        validate_simulator_extensions(world)
        proximity_model_id = selected_models(world.model_configs).get("social_proximity")
        if proximity_model_id is not None:
            self.proximity_policy = create_installed_model("social_proximity", world.model_configs)
        else:
            legacy_config = canonical_proximity_contact_config(
                world.model_configs.get("social_proximity")
            )
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
            "sleep_debt_utility_weight": behavior_config.get("sleep_debt_utility_weight", 1.0),
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
                "effect_program_temporary_space_expire": EffectProgramTemporarySpaceHandler(
                    expire=True
                ),
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
        # No phase edits model_configs in place, and a phase that swaps the object brings
        # new raw objects, so what was parsed is kept while the object and its values stay
        # the same. Comparing the object too keeps swaps from piling up old configs.
        if minute != self._config_scopes_minute:
            self._config_scopes_minute = minute
            # The stamp says no write reached the object, without reading it.
            stamp = field_stamp(world, "model_configs")
            unwritten = same_stamp(stamp, self._config_scopes_stamp)
            self._config_scopes_stamp = stamp
            if not unwritten and (
                world.model_configs is not self._config_scopes_object
                or world.model_configs != self._config_scopes_configs
            ):
                self._config_scopes_object = world.model_configs
                self._config_scopes_configs = deepcopy(world.model_configs)
                self._parsed_configs.clear()
                self._model_selections.clear()
                self._room_routes = {"routes": {}}
                self._zone_environments = {}
        with (
            geometry_config_scope(self._parsed_configs),
            selected_models_scope(self._model_selections),
            room_route_scope(self._room_routes),
        ):
            self._run_phase(phase, world, minute, rng)

    def _run_phase(
        self, phase: TickPhase, world: WorldState, minute: int, rng: KeyedRandom
    ) -> None:
        if world is not self.world:
            raise ValueError("offline simulation controller is bound to a different WorldState")
        if phase is TickPhase.DISPATCH_DUE_EVENTS:
            self._run_dispatch_phase(world, minute)
        elif phase is TickPhase.UPDATE_AGENT_STATE:
            self._run_update_phase(world, minute, rng)
        elif phase is TickPhase.PROPOSE_ACTIONS:
            self._run_propose_phase(world, minute, rng)
        elif phase is TickPhase.ARBITRATE_ACTIONS:
            self._run_arbitrate_phase(world, minute, rng)
        elif phase is TickPhase.SAMPLE_ENDOGENOUS_EVENTS:
            self._run_sample_phase(minute, rng)
        elif phase is TickPhase.COLLECT_METRICS:
            self._run_metrics_phase(world, minute)


install_segments(OfflineSimulationController)


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
    if (
        checkpoint_at_minute is not None
        and not 0 < checkpoint_at_minute < scenario.duration_minutes
    ):
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
