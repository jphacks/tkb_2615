"""The generator records which model implementations a new Scenario uses, and the
parameter overrides a request selects reach the runtime config, provenance and the
Checkpoint signature.

These check the generator itself, so they build drafts directly. That a generated
Scenario's people survive frames and checkpoints is checked in
tests/unit/web/test_frames.py.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from sheltersim.contracts.extensions import Point2D
from sheltersim.contracts.policies import model_catalog, model_signatures
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.domain.environment import (
    ScenarioZoneEnvironmentConfig,
    zone_environment_view,
)
from sheltersim.domain.models import Activity
from sheltersim.domain.movement import in_room_travel_minutes
from sheltersim.domain.resource_service import ResourceServiceManager
from sheltersim.domain.staff_dynamics import (
    STAFF_DYNAMICS_STATE_KEY,
    staff_dynamics_on_shift,
    update_staff_dynamics,
)
from sheltersim.domain.staff_operations import STAFF_OPERATIONS_STATE_KEY
from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioRequest,
)
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.validation import (
    EvaluationVariant,
    _variant_scenario,
    parameter_catalog,
)
from sheltersim.policies.alternatives import DeGrootContactPolicy, HazardInformationPolicy
from sheltersim.policies.behavior import (
    PriorityRuleBehaviorPolicy,
    UtilityBehaviorPolicy,
    WorldView,
)
from sheltersim.policies.queueing import create_queue_policy
from sheltersim.policies.registry import (
    create_behavior,
    create_body_dynamics_formula,
    create_care_outcome_formula,
    create_information,
    create_psychology,
    create_social,
    create_staff_fatigue_formula,
)
from sheltersim.region.conditions import (
    InitialResourceCondition,
    OperationalConditions,
    StaffShiftInput,
)


def _request(**policies: str) -> InitialScenarioRequest:
    return InitialScenarioRequest(
        municipality="Synthetic municipality",
        disaster="earthquake",
        evacuees=12,
        duration_days=1,
        seed=42,
        **policies,
    )


def test_generated_scenarios_select_the_default_policies() -> None:
    scenario = InitialScenarioBuilder().build(_request()).scenario

    context = scenario.model_configs["population_context"]
    assert len(context["people"]) == 12
    assert all(person["medication_support"] is None for person in context["people"].values())
    registry = scenario.model_configs["policy_registry"]
    assert registry["body_dynamics"] == "body-feedback-complete-v2"
    assert registry["staff_operations"] == "staff-urgency-first-v1"
    assert registry["staff_fatigue"] == "staff-fatigue-linear-v1"
    assert registry["zone_environment"] == "zone-temperature-comfort-band-v1"
    assert registry["zone_rest"] == "zone-rest-multiplicative-v1"
    assert registry["zone_hygiene"] == "zone-hygiene-linear-v1"
    assert registry["zone_privacy"] == "zone-privacy-threshold-v1"


def test_requested_policies_reach_the_world_and_its_checkpoint() -> None:
    scenario = (
        InitialScenarioBuilder()
        .build(
            _request(
                body_dynamics_policy_id="body-feedback-sleep-debt-v1",
                staff_operations_policy_id="staff-travel-first-v1",
                staff_fatigue_policy_id="staff-fatigue-saturating-v1",
                zone_temperature_policy_id="zone-temperature-midpoint-v1",
                zone_rest_policy_id="zone-rest-limiting-v1",
                zone_hygiene_policy_id="zone-hygiene-quadratic-v1",
                zone_privacy_policy_id="zone-privacy-proportional-v1",
            )
        )
        .scenario
    )

    world = WorldState.from_scenario(scenario)
    assert world.model_configs["policy_registry"]["body_dynamics"] == (
        "body-feedback-sleep-debt-v1"
    )
    assert world.model_configs["policy_registry"]["zone_hygiene"] == "zone-hygiene-quadratic-v1"
    assert "policy:staff_fatigue" in Checkpoint.capture(world).model_versions


def _override_request(**overrides: object) -> InitialScenarioRequest:
    return InitialScenarioRequest(
        municipality="Synthetic municipality",
        disaster="earthquake",
        evacuees=8,
        duration_days=1,
        seed=7,
        **overrides,
    )


def test_selected_formula_overrides_reach_runtime_config_and_checkpoint_signature() -> None:
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request())
    customized = builder.build(_override_request(
        behavior_policy_id="priority-rule-v1",
        social_policy_id="degroot-contact-v1",
        information_policy_id="hazard-information-v1",
        social_propagation_enabled=True,
        information_spread_enabled=True,
        care_outcome_policy_id="hygiene-support-proportional-relief-v1",
        model_parameter_overrides={
            "behavior.temperature": 0.35,
            "body_dynamics.sleep_debt_stress_rate": 0.004,
            "facility_queues.retry_delay_minutes": 7,
            "social_propagation.stress_spread_per_minute": 0.003,
            "information_spread.base_transmission_probability": 0.35,
            "staff_dynamics.max_continuous_work_minutes": 300,
            "zone_environment.temperature_stress_per_degree_minute": 0.0003,
            "body_dynamics.hygiene_support_relief": 0.3,
        }
    ))
    environment_config = dict(customized.scenario.model_configs["zone_environment"])
    environment_config["baseline_by_zone"] = {"sleeping": {"temperature_c": 30.0}}
    customized.scenario.model_configs["zone_environment"] = (
        ScenarioZoneEnvironmentConfig.model_validate(environment_config).model_dump(mode="json")
    )

    config = customized.scenario.model_configs
    assert config["body_dynamics"]["sleep_debt_stress_rate"] == 0.004
    assert config["staff_dynamics"]["max_continuous_work_minutes"] == 300
    assert config["zone_environment"]["temperature_stress_per_degree_minute"] == 0.0003
    assert config["body_dynamics"]["hygiene_support_relief"] == 0.3
    assert config["behavior"]["temperature"] == 0.35
    assert config["facility_queues"]["retry_delay_minutes"] == 7
    assert config["social_propagation"]["enabled"] is True
    assert config["social_propagation"]["stress_spread_per_minute"] == 0.003
    assert config["information_spread"]["enabled"] is True
    assert config["information_spread"]["base_transmission_probability"] == 0.35
    assert isinstance(create_behavior(config), PriorityRuleBehaviorPolicy)
    assert create_queue_policy(config).retry_delay_minutes() == 7
    assert isinstance(create_social(config), DeGrootContactPolicy)
    assert isinstance(create_information(config), HazardInformationPolicy)
    class ArtifactSink:
        trace_level = TraceLevel.OFF

        def record_decision(self, *_args: object, **_kwargs: object) -> None:
            pass

    controller = OfflineSimulationController(
        WorldState.from_scenario(customized.scenario), ArtifactSink()
    )
    assert isinstance(controller.decisions.policy, PriorityRuleBehaviorPolicy)
    assert controller.decisions.policy.scorer.temperature == 0.35
    assert isinstance(controller.stress_policy, DeGrootContactPolicy)
    assert controller.stress_policy.parameters.enabled is True
    assert controller.stress_policy.parameters.stress_spread_per_minute == 0.003
    assert isinstance(controller.information_policy, HazardInformationPolicy)
    fixed_care = create_care_outcome_formula(baseline.scenario.model_configs)
    proportional_care = create_care_outcome_formula(config)
    assert fixed_care is not None and proportional_care is not None
    assert fixed_care.hygiene_need_after_support(0.8, 0.15) == pytest.approx(0.65)
    assert proportional_care.hygiene_need_after_support(0.8, 0.3) == pytest.approx(0.56)
    baseline_formula = create_body_dynamics_formula(baseline.scenario.model_configs)
    customized_formula = create_body_dynamics_formula(customized.scenario.model_configs)
    inputs = {
        "sleep_debt_minutes": 720,
        "uncertainty": 0,
        "discontent": 0,
        "hygiene_need": 0,
        "sanitation_level": 1,
        "family_separation_pressure": 0,
        "reassurance": 0,
        "positive_affect": 0,
    }
    base_output = baseline_formula.stress_feedback(
        **inputs, config=baseline.scenario.model_configs["body_dynamics"]
    )
    changed_output = customized_formula.stress_feedback(
        **inputs, config=config["body_dynamics"]
    )
    assert changed_output == pytest.approx(base_output * 4)
    baseline_fatigue = create_staff_fatigue_formula(
        baseline.scenario.model_configs
    ).fatigue(150, baseline.scenario.model_configs["staff_dynamics"]["max_continuous_work_minutes"])
    changed_fatigue = create_staff_fatigue_formula(config).fatigue(
        150, config["staff_dynamics"]["max_continuous_work_minutes"]
    )
    assert baseline_fatigue == pytest.approx(150 / 240)
    assert changed_fatigue == pytest.approx(150 / 300)

    before = WorldState.from_scenario(baseline.scenario)
    after = WorldState.from_scenario(customized.scenario)
    assert zone_environment_view(before, "sleeping").temperature_stress_per_minute == 0
    assert zone_environment_view(after, "sleeping").temperature_stress_per_minute == pytest.approx(0.0012)
    baseline_checkpoint = Checkpoint.capture(before)
    customized_checkpoint = Checkpoint.capture(after)
    for policy in (
        "behavior", "psychology", "body_dynamics", "queue", "social", "information",
        "staff_fatigue", "zone_environment", "care_outcome",
    ):
        assert baseline_checkpoint.model_versions[f"policy:{policy}"] != (
            customized_checkpoint.model_versions[f"policy:{policy}"]
        )
    restored = Checkpoint.from_json(customized_checkpoint.to_json()).restore_world()
    assert restored.canonical_json() == after.canonical_json()

    recorded = [
        row for row in customized.scenario.provenance["assumptions"]
        if row.get("id", "").startswith("ASM-POLICY-OVERRIDE-")
    ]
    assert {row["subject"]: row["value"] for row in recorded} == config_override_values


config_override_values = {
    "behavior.temperature": 0.35,
    "body_dynamics.sleep_debt_stress_rate": 0.004,
    "facility_queues.retry_delay_minutes": 7,
    "social_propagation.stress_spread_per_minute": 0.003,
    "information_spread.base_transmission_probability": 0.35,
    "staff_dynamics.max_continuous_work_minutes": 300,
    "zone_environment.temperature_stress_per_degree_minute": 0.0003,
    "body_dynamics.hygiene_support_relief": 0.3,
}


def test_parameter_overrides_reject_unselected_coefficients_and_catalog_bound_violations() -> None:
    with pytest.raises(ValueError, match="finite JSON numbers"):
        _override_request(model_parameter_overrides={
            "body_dynamics.sleep_debt_stress_rate": True,
        })

    with pytest.raises(ValueError, match="not consumed by a selected installed formula"):
        InitialScenarioBuilder().build(_override_request(
            model_parameter_overrides={
                "body_dynamics.unregistered_stress_rate": 0.02,
            },
        ))

    with pytest.raises(ValueError, match="outside its Model Catalog bounds"):
        InitialScenarioBuilder().build(_override_request(
            model_parameter_overrides={
                "body_dynamics.sleep_debt_stress_rate": 2.0,
            },
        ))

    with pytest.raises(ValueError, match="requires an integer value"):
        InitialScenarioBuilder().build(_override_request(
            model_parameter_overrides={
                "staff_dynamics.max_continuous_work_minutes": 300.5,
            },
        ))


def test_per_staff_fatigue_parameter_is_typed_applied_and_checkpoint_signed() -> None:
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request())
    staff_id = baseline.scenario.staff[0].id
    customized = builder.build(_override_request(model_parameter_overrides={
        "staff_dynamics.max_continuous_work_minutes_by_staff.*": {staff_id: 120},
    }))
    config = customized.scenario.model_configs
    assert config["staff_dynamics"]["max_continuous_work_minutes_by_staff"] == {staff_id: 120}

    formula = create_staff_fatigue_formula(config)
    assert formula.maximum_minutes(240, staff_id) == 120
    assert formula.maximum_minutes(240, "other-staff") == 240
    assert formula.fatigue(60, formula.maximum_minutes(240, staff_id)) == pytest.approx(0.5)

    running_world = WorldState.from_scenario(customized.scenario)
    running_world.staff[staff_id].active_task_id = "synthetic-active-task"
    runtime_record = running_world.model_state[STAFF_DYNAMICS_STATE_KEY]["staff"][staff_id]
    runtime_record["consecutive_work_minutes"] = 59
    update_staff_dynamics(running_world, 0)
    assert runtime_record["fatigue"] == pytest.approx(0.5)

    baseline_world = WorldState.from_scenario(baseline.scenario)
    assert "max_continuous_work_minutes_by_staff" not in baseline_world.model_state[
        STAFF_DYNAMICS_STATE_KEY
    ]["config"]
    before = Checkpoint.capture(baseline_world)
    after = Checkpoint.capture(WorldState.from_scenario(customized.scenario))
    assert before.model_versions["policy:staff_fatigue"] != after.model_versions["policy:staff_fatigue"]
    legacy_configs = deepcopy(baseline.scenario.model_configs)
    legacy_configs["staff_dynamics"].pop("max_continuous_work_minutes_by_staff")
    assert model_signatures(legacy_configs)["policy:staff_fatigue"] == (
        before.model_versions["policy:staff_fatigue"]
    )
    assert Checkpoint.from_json(after.to_json()).restore_world().canonical_json() == (
        WorldState.from_scenario(customized.scenario).canonical_json()
    )
    applied = [
        row for row in customized.scenario.provenance["assumptions"]
        if row.get("subject") == "staff_dynamics.max_continuous_work_minutes_by_staff.*"
    ]
    assert len(applied) == 1
    assert applied[0]["value"] == {staff_id: 120}
    assert applied[0]["applicability"].startswith("Optional experimental work-duration threshold")

    with pytest.raises(ValueError, match="unknown staff IDs"):
        builder.build(_override_request(model_parameter_overrides={
            "staff_dynamics.max_continuous_work_minutes_by_staff.*": {"unknown-staff": 120},
        }))
    with pytest.raises(ValueError, match="outside its Model Catalog bounds"):
        builder.build(_override_request(model_parameter_overrides={
            "staff_dynamics.max_continuous_work_minutes_by_staff.*": {staff_id: 1441},
        }))
    with pytest.raises(ValueError, match="integer staff-specific minutes"):
        builder.build(_override_request(model_parameter_overrides={
            "staff_dynamics.max_continuous_work_minutes_by_staff.*": {staff_id: 120.5},
        }))


def test_resident_specific_behavior_cadence_reaches_policy_and_checkpoint() -> None:
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request())
    agent_id = baseline.scenario.profiles[0].id
    path = "behavior.decision_interval_minutes_by_agent.*"
    customized = builder.build(_override_request(model_parameter_overrides={
        path: {agent_id: 30},
        "behavior.temperature_by_agent.*": {agent_id: 0.35},
    }))

    assert customized.scenario.model_configs["behavior"][
        "decision_interval_minutes_by_agent"
    ] == {agent_id: 30}
    assert customized.scenario.model_configs["behavior"]["temperature_by_agent"] == {
        agent_id: 0.35,
    }
    baseline_world = WorldState.from_scenario(baseline.scenario)
    customized_world = WorldState.from_scenario(customized.scenario)
    baseline_checkpoint = Checkpoint.capture(baseline_world)
    customized_checkpoint = Checkpoint.capture(customized_world)
    assert baseline_checkpoint.model_versions["policy:behavior"] != (
        customized_checkpoint.model_versions["policy:behavior"]
    )

    legacy_configs = deepcopy(baseline.scenario.model_configs)
    legacy_configs["behavior"]["decision_interval_minutes_by_agent"] = {}
    legacy_configs["behavior"]["temperature_by_agent"] = {}
    assert model_signatures(legacy_configs)["policy:behavior"] == (
        baseline_checkpoint.model_versions["policy:behavior"]
    )

    class ArtifactSink:
        trace_level = TraceLevel.OFF

        def record_decision(self, *_args: object, **_kwargs: object) -> None:
            pass

    controller = OfflineSimulationController(customized_world, ArtifactSink())
    policy = controller.decisions.policy
    assert isinstance(policy, UtilityBehaviorPolicy)
    assert policy.decision_interval_minutes_by_agent == {agent_id: 30}
    assert policy.temperature_by_agent == {agent_id: 0.35}
    customized_world_json = customized_world.canonical_json()
    customized_world.agents[agent_id].next_decision_at = 0
    view = WorldView.from_world(customized_world)

    class FixedRandom:
        def random(self, *_parts: object) -> float:
            return 0.0

    proposal = policy.propose(view, agent_id, FixedRandom())
    assert proposal.next_decision_at == 30
    other_agent_id = customized.scenario.profiles[1].id
    customized_world.agents[other_agent_id].next_decision_at = 0
    other_proposal = policy.propose(
        WorldView.from_world(customized_world), other_agent_id, FixedRandom()
    )
    # The unmodified resident keeps the suite's shared 15-minute decision
    # interval; resident-specific overrides apply only to A001.
    assert other_proposal.next_decision_at == 15
    restored = Checkpoint.from_json(customized_checkpoint.to_json()).restore_world()
    assert restored.canonical_json() == customized_world_json

    with pytest.raises(ValueError, match="integer Agent values"):
        builder.build(_override_request(model_parameter_overrides={
            path: {agent_id: 30.5},
        }))
    with pytest.raises(ValueError, match="unknown Agent IDs"):
        builder.build(_override_request(model_parameter_overrides={
            path: {"unknown-agent": 30},
        }))
    with pytest.raises(ValueError, match="not consumed by a selected installed formula"):
        builder.build(_override_request(
            behavior_policy_id="priority-rule-v1",
            model_parameter_overrides={
                "behavior.temperature_by_agent.*": {agent_id: 0.35},
            },
        ))


def test_mobility_speed_override_reaches_routes_provenance_and_checkpoint() -> None:
    import json

    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request())
    agent_id = baseline.scenario.profiles[0].id
    path = "movement_model.speed_m_per_minute_by_mobility.assisted"
    customized = builder.build(_override_request(model_parameter_overrides={path: 18.0}))

    def with_assisted_profile(scenario: ScenarioSpec) -> ScenarioSpec:
        document = json.loads(scenario.canonical_json())
        document["profiles"][0]["mobility"] = "assisted"
        return ScenarioSpec.model_validate(document)

    baseline_scenario = with_assisted_profile(baseline.scenario)
    customized_scenario = with_assisted_profile(customized.scenario)
    baseline_world = WorldState.from_scenario(baseline_scenario)
    customized_world = WorldState.from_scenario(customized_scenario)
    route_points = (Point2D(x=0, y=0), Point2D(x=72, y=0))
    assert in_room_travel_minutes(customized_world, agent_id, route_points) > (
        in_room_travel_minutes(baseline_world, agent_id, route_points)
    )
    assert customized_scenario.model_configs["movement_model"][
        "speed_m_per_minute_by_mobility"
    ]["assisted"] == 18.0

    baseline_checkpoint = Checkpoint.capture(baseline_world)
    customized_checkpoint = Checkpoint.capture(customized_world)
    assert baseline_checkpoint.model_versions["movement_model"] != (
        customized_checkpoint.model_versions["movement_model"]
    )
    restored = Checkpoint.from_json(customized_checkpoint.to_json()).restore_world()
    assert restored.canonical_json() == customized_world.canonical_json()
    applied = next(
        row for row in customized.scenario.provenance["assumptions"]
        if row.get("subject") == path
    )
    assert applied["value"] == 18.0
    assert applied["unit"] == "metre_per_minute"

    with pytest.raises(ValueError, match="Model Catalog bounds"):
        builder.build(_override_request(model_parameter_overrides={path: 0.0}))


def test_resource_service_coefficients_reach_runtime_and_checkpoint() -> None:
    conditions = OperationalConditions(
        initial_resources=InitialResourceCondition(
            status="entered",
            water_ml=5_000,
            food_meal_units=20,
        )
    )
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request(operational_conditions=conditions))
    customized = builder.build(_override_request(
        operational_conditions=conditions,
        model_parameter_overrides={
            "resource_service.hydration_relief": 0.2,
            "resource_service.service_minutes": 8,
            "resource_service.water_utility_need_weight": 0.4,
        },
    ))
    config = customized.scenario.model_configs["resource_service"]
    assert config["hydration_relief"] == 0.2
    assert config["service_minutes"] == 8
    assert config["water_utility_need_weight"] == 0.4

    class ArtifactSink:
        trace_level = TraceLevel.OFF

        def record_decision(self, *_args: object, **_kwargs: object) -> None:
            pass

    baseline_world = WorldState.from_scenario(baseline.scenario)
    customized_world = WorldState.from_scenario(customized.scenario)
    manager = ResourceServiceManager(customized_world)
    assert manager.config["hydration_relief"] == 0.2
    controller = OfflineSimulationController(customized_world, ArtifactSink())
    assert controller.decisions.policy.water_utility_need_weight == 0.4

    baseline_checkpoint = Checkpoint.capture(baseline_world)
    customized_checkpoint = Checkpoint.capture(customized_world)
    assert baseline_checkpoint.model_versions["resource_service"] != (
        customized_checkpoint.model_versions["resource_service"]
    )
    restored = Checkpoint.from_json(customized_checkpoint.to_json()).restore_world()
    assert restored.canonical_json() == customized_world.canonical_json()
    overrides = {
        row["subject"]: row
        for row in customized.scenario.provenance["assumptions"]
        if row.get("subject", "").startswith("resource_service.")
    }
    assert set(overrides) == {
        "resource_service.hydration_relief",
        "resource_service.service_minutes",
        "resource_service.water_utility_need_weight",
    }
    assert all(
        row["source_id"] == config["source_id"] and row["evidence_grade"] == "C"
        for row in overrides.values()
    )

    with pytest.raises(ValueError, match="Model Catalog bounds"):
        builder.build(_override_request(
            operational_conditions=conditions,
            model_parameter_overrides={"resource_service.hydration_relief": 0.0},
        ))


def test_toilet_service_duration_overrides_reach_controller_and_checkpoint() -> None:
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request())
    customized = builder.build(_override_request(model_parameter_overrides={
        "toilet_service.minimum_minutes": 6,
        "toilet_service.maximum_minutes": 6,
    }))
    config = customized.scenario.model_configs["toilet_service"]
    assert config["minimum_minutes"] == config["maximum_minutes"] == 6
    entries = {row["path"]: row for row in parameter_catalog(customized.scenario)}
    for path in ("toilet_service.minimum_minutes", "toilet_service.maximum_minutes"):
        assert entries[path]["value"] == 6
        assert entries[path]["source_id"] == config["source_id"]
        assert entries[path]["unit"] == "minute"

    class ArtifactSink:
        trace_level = TraceLevel.OFF

        def record_decision(self, *_args: object, **_kwargs: object) -> None:
            pass

    world = WorldState.from_scenario(customized.scenario)
    controller = OfflineSimulationController(world, ArtifactSink())
    assert controller.toilet_service.min_service_minutes == 6
    assert controller.toilet_service.max_service_minutes == 6
    before = Checkpoint.capture(WorldState.from_scenario(baseline.scenario))
    checkpoint = Checkpoint.capture(world)
    assert before.model_versions["toilet_service"] != checkpoint.model_versions["toilet_service"]
    restored = Checkpoint.from_json(checkpoint.to_json()).restore_world()
    assert restored.canonical_json() == world.canonical_json()

    with pytest.raises(ValueError, match="outside its Model Catalog bounds"):
        builder.build(
            _override_request(model_parameter_overrides={"toilet_service.minimum_minutes": 1441})
        )
    with pytest.raises(ValueError, match="maximum_minutes must be at least minimum_minutes"):
        builder.build(_override_request(model_parameter_overrides={
            "toilet_service.minimum_minutes": 9,
            "toilet_service.maximum_minutes": 8,
        }))


def test_shift_dynamics_overrides_require_v2_and_change_runtime_recovery() -> None:
    builder = InitialScenarioBuilder()
    legacy_rest = builder.build(_override_request(model_parameter_overrides={
        "staff_dynamics.rest_minutes": 45,
    }))
    legacy_world = WorldState.from_scenario(legacy_rest.scenario)
    legacy_record = legacy_world.model_state[STAFF_DYNAMICS_STATE_KEY]["staff"]["staff_001"]
    legacy_record["consecutive_work_minutes"] = 240
    update_staff_dynamics(legacy_world, 0)
    assert legacy_record["rest_until_minute"] == 45

    shifts = OperationalConditions(staff_shifts=(
        StaffShiftInput(staff_id="staff_001", start_minute=60, end_minute=300),
    ))
    baseline = builder.build(_override_request(operational_conditions=shifts))
    customized = builder.build(_override_request(
        operational_conditions=shifts,
        model_parameter_overrides={
            "staff_dynamics.rest_minutes": 45,
            "staff_dynamics.off_shift_fatigue_recovery_per_minute": 0.01,
        },
    ))
    config = customized.scenario.model_configs["staff_dynamics"]
    assert config["policy_version"] == "continuous-duty-fatigue-v2"
    assert config["rest_minutes"] == 45
    assert config["off_shift_fatigue_recovery_per_minute"] == 0.01
    world = WorldState.from_scenario(customized.scenario)
    record = world.model_state[STAFF_DYNAMICS_STATE_KEY]["staff"]["staff_001"]
    record["fatigue"] = 0.5
    record["consecutive_work_minutes"] = 20
    update_staff_dynamics(world, 0)
    assert record["fatigue"] == pytest.approx(0.49)
    record["consecutive_work_minutes"] = config["max_continuous_work_minutes"]
    update_staff_dynamics(world, 60)
    assert record["rest_until_minute"] == 105

    base_checkpoint = Checkpoint.capture(WorldState.from_scenario(baseline.scenario))
    checkpoint = Checkpoint.capture(WorldState.from_scenario(customized.scenario))
    assert base_checkpoint.model_versions["staff_dynamics"] != checkpoint.model_versions[
        "staff_dynamics"
    ]
    assert Checkpoint.from_json(checkpoint.to_json()).restore_world().canonical_json() == (
        WorldState.from_scenario(customized.scenario).canonical_json()
    )
    with pytest.raises(ValueError, match="not consumed by a selected installed formula"):
        builder.build(_override_request(model_parameter_overrides={
            "staff_dynamics.off_shift_fatigue_recovery_per_minute": 0.01
        }))


def test_staff_support_lifecycle_overrides_reach_controller_and_checkpoint() -> None:
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request())
    customized = builder.build(_override_request(model_parameter_overrides={
        "staff.support_task_minutes": 12,
        "staff.unresolved_after_minutes": 120,
        "staff.support_cooldown_minutes": 40,
        "staff.support_fairness_minutes": 60,
    }))
    config = customized.scenario.model_configs["staff"]
    assert config["support_task_minutes"] == 12
    assert config["unresolved_after_minutes"] == 120
    assert config["support_cooldown_minutes"] == 40
    assert config["support_fairness_minutes"] == 60
    entries = {row["path"]: row for row in parameter_catalog(customized.scenario)}
    for name in config:
        path = f"staff.{name}"
        if name not in {"source_type", "source_id"}:
            assert path in entries
            assert entries[path]["value"] == config[name]
            assert entries[path]["source_id"] == config["source_id"]

    class ArtifactSink:
        trace_level = TraceLevel.OFF

        def record_decision(self, *_args: object, **_kwargs: object) -> None:
            pass

    world = WorldState.from_scenario(customized.scenario)
    controller = OfflineSimulationController(world, ArtifactSink())
    assert controller.staff_service.task_duration_minutes == 12
    assert controller.staff_service.unresolved_after_minutes == 120
    assert controller.staff_service.cooldown_minutes == 40
    assert controller.staff_service.fairness_after_minutes == 60
    baseline_checkpoint = Checkpoint.capture(WorldState.from_scenario(baseline.scenario))
    checkpoint = Checkpoint.capture(world)
    assert baseline_checkpoint.model_versions["staff"] != checkpoint.model_versions["staff"]
    assert Checkpoint.from_json(checkpoint.to_json()).restore_world().canonical_json() == (
        world.canonical_json()
    )
    with pytest.raises(ValueError, match="no greater than the unresolved timeout"):
        builder.build(_override_request(model_parameter_overrides={
            "staff.unresolved_after_minutes": 60,
            "staff.support_fairness_minutes": 90,
        }))


def test_per_service_queue_wait_limit_override_changes_runtime_and_checkpoint_signature() -> None:
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request())
    customized = builder.build(_override_request(model_parameter_overrides={
        "facility_queues.abandon_after_minutes_by_service.*": {"toilet": 60},
    }))
    config = customized.scenario.model_configs
    assert config["facility_queues"]["abandon_after_minutes_by_service"] == {"toilet": 60}
    assert create_queue_policy(config).model_id == "need-weighted-physical-queue-v1"

    world = WorldState.from_scenario(customized.scenario)
    agent_id = next(iter(world.agents))
    world.agents[agent_id].needs.toilet_urgency = 0.0
    world.agents[agent_id].needs.hydration_need = 0.0
    world.agents[agent_id].needs.hunger = 0.0
    queue = create_queue_policy(config)
    assert queue.should_abandon(world, agent_id, "toilet", 59) is False
    assert queue.should_abandon(world, agent_id, "toilet", 60) is True
    assert queue.should_abandon(world, agent_id, "water", 239) is False
    assert queue.should_abandon(world, agent_id, "water", 240) is True

    baseline_world = WorldState.from_scenario(baseline.scenario)
    baseline_checkpoint = Checkpoint.capture(baseline_world)
    customized_checkpoint = Checkpoint.capture(world)
    assert baseline_checkpoint.model_versions["policy:queue"] != (
        customized_checkpoint.model_versions["policy:queue"]
    )
    restored = Checkpoint.from_json(customized_checkpoint.to_json()).restore_world()
    assert restored.canonical_json() == world.canonical_json()
    applied = [
        row for row in customized.scenario.provenance["assumptions"]
        if row.get("subject") == "facility_queues.abandon_after_minutes_by_service.*"
    ]
    assert len(applied) == 1
    assert applied[0]["value"] == {"toilet": 60}
    catalog_entry = model_catalog()["need-weighted-physical-queue-v1"]["parameters"][
        "facility_queues.abandon_after_minutes_by_service.*"
    ]
    assert catalog_entry["scope"] == "per_service"
    assert catalog_entry["unit"] == "minute"
    assert "scope" not in model_catalog()["utility-softmax-v1"]["parameters"][
        "behavior.temperature"
    ]

    with pytest.raises(ValueError, match="unsupported service names"):
        builder.build(_override_request(model_parameter_overrides={
            "facility_queues.abandon_after_minutes_by_service.*": {"electricity": 60},
        }))
    with pytest.raises(ValueError, match="outside its Model Catalog bounds"):
        builder.build(_override_request(model_parameter_overrides={
            "facility_queues.abandon_after_minutes_by_service.*": {"toilet": 10081},
        }))
    with pytest.raises(ValueError, match="integer service minutes"):
        builder.build(_override_request(model_parameter_overrides={
            "facility_queues.abandon_after_minutes_by_service.*": {"toilet": 60.5},
        }))
    with pytest.raises(ValueError, match="not consumed by a selected installed formula"):
        builder.build(_override_request(
            queue_policy_id="strict-fifo-queue-v1",
            model_parameter_overrides={
                "facility_queues.abandon_after_minutes_by_service.*": {"toilet": 60},
            },
        ))


def test_per_zone_environment_baseline_override_is_typed_runtime_and_checkpoint_signed() -> None:
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request())
    customized = builder.build(_override_request(model_parameter_overrides={
        "zone_environment.baseline_by_zone.*": {
            "sleeping": {
                "ventilation_effectiveness": 0.3,
                "lighting_level": 0.8,
                "privacy_level": 0.2,
                "temperature_c": 30.0,
            },
        },
    }))
    config = customized.scenario.model_configs
    assert config["zone_environment"]["baseline_by_zone"]["sleeping"] == {
        "ventilation_effectiveness": 0.3,
        "lighting_level": 0.8,
        "privacy_level": 0.2,
        "temperature_c": 30.0,
    }

    baseline_view = zone_environment_view(WorldState.from_scenario(baseline.scenario), "sleeping")
    view = zone_environment_view(WorldState.from_scenario(customized.scenario), "sleeping")
    assert view.ventilation_effectiveness == 0.3
    assert view.lighting_level == 0.8
    assert view.privacy_level == 0.2
    assert view.temperature_c == 30.0
    assert view.temperature_stress_per_minute > 0
    assert view.rest_efficiency_multiplier < baseline_view.rest_efficiency_multiplier
    assert view.ventilation_hygiene_growth_per_minute > baseline_view.ventilation_hygiene_growth_per_minute
    assert view.privacy_discontent_per_minute > baseline_view.privacy_discontent_per_minute

    baseline_world = WorldState.from_scenario(baseline.scenario)
    customized_world = WorldState.from_scenario(customized.scenario)
    before = Checkpoint.capture(baseline_world)
    after = Checkpoint.capture(customized_world)
    assert before.model_versions["policy:zone_environment"] != (
        after.model_versions["policy:zone_environment"]
    )
    restored = Checkpoint.from_json(after.to_json()).restore_world()
    assert restored.canonical_json() == customized_world.canonical_json()

    provenance = [
        row for row in customized.scenario.provenance["assumptions"]
        if row.get("subject") == "zone_environment.baseline_by_zone.*"
    ]
    assert len(provenance) == 1
    assert provenance[0]["value"] == {
        "sleeping": {
            "ventilation_effectiveness": 0.3,
            "lighting_level": 0.8,
            "privacy_level": 0.2,
            "temperature_c": 30.0,
        },
    }
    assert provenance[0]["scope"] == "per_zone_id"
    assert provenance[0]["field_units"]["temperature_c"] == "degree_celsius"
    assert provenance[0]["field_bounds"]["temperature_c"] == [-40.0, 60.0]
    catalog_entry = next(
        entry["parameters"]["zone_environment.baseline_by_zone.*"]
        for entry in model_catalog().values()
        if "zone_environment.baseline_by_zone.*" in entry["parameters"]
    )
    assert catalog_entry["scope"] == "per_zone_id"
    assert catalog_entry["field_units"]["temperature_c"] == "degree_celsius"

    with pytest.raises(ValueError, match="unknown zone IDs"):
        builder.build(_override_request(model_parameter_overrides={
            "zone_environment.baseline_by_zone.*": {"unknown-zone": {"privacy_level": 0.8}},
        }))
    with pytest.raises(ValueError, match="unsupported per-zone fields"):
        builder.build(_override_request(model_parameter_overrides={
            "zone_environment.baseline_by_zone.*": {"sleeping": {"elevation_m": 3.0}},
        }))
    with pytest.raises(ValueError, match="outside its Model Catalog bounds"):
        builder.build(_override_request(model_parameter_overrides={
            "zone_environment.baseline_by_zone.*": {"sleeping": {"lighting_level": 1.1}},
        }))


def test_per_zone_staff_task_thresholds_are_provenanced_consumed_and_signed() -> None:
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request())
    customized = builder.build(_override_request(model_parameter_overrides={
        "staff_operations.thresholds_by_zone.*": {
            "sleeping": {
                "cleaning_sanitation_below": 0.72,
                "outreach_uncertainty_at_least": 0.51,
                "patrol_stress_at_least": 0.55,
            },
        },
    }))
    base_config = baseline.scenario.model_configs
    config = customized.scenario.model_configs
    assert config["staff_operations"]["thresholds_by_zone"] == {
        "sleeping": {
            "cleaning_sanitation_below": 0.72,
            "outreach_uncertainty_at_least": 0.51,
            "patrol_stress_at_least": 0.55,
        },
    }

    base_world = WorldState.from_scenario(baseline.scenario)
    customized_world = WorldState.from_scenario(customized.scenario)
    legacy_configs = deepcopy(base_world.model_configs)
    legacy_configs["staff_operations"].pop("thresholds_by_zone")
    assert model_signatures(legacy_configs)["policy:staff_operations"] == (
        model_signatures(base_world.model_configs)["policy:staff_operations"]
    )
    legacy_world = WorldState.from_scenario(baseline.scenario)
    legacy_world.model_configs["staff_operations"].pop("thresholds_by_zone")
    legacy_world.model_state[STAFF_OPERATIONS_STATE_KEY]["config"].pop(
        "thresholds_by_zone"
    )
    legacy_world.validate()
    legacy_checkpoint = Checkpoint.capture(legacy_world)
    assert Checkpoint.from_json(legacy_checkpoint.to_json()).restore_world().canonical_json() == (
        legacy_world.canonical_json()
    )
    before = Checkpoint.capture(base_world)
    after = Checkpoint.capture(customized_world)
    assert before.model_versions["policy:staff_operations"] != (
        after.model_versions["policy:staff_operations"]
    )
    restored = Checkpoint.from_json(after.to_json()).restore_world()
    assert restored.canonical_json() == customized_world.canonical_json()

    provenance = [
        row for row in customized.scenario.provenance["assumptions"]
        if row.get("subject") == "staff_operations.thresholds_by_zone.*"
    ]
    assert len(provenance) == 1
    assert provenance[0]["value"] == config["staff_operations"]["thresholds_by_zone"]
    assert provenance[0]["scope"] == "per_zone_id"

    assert "thresholds_by_zone" not in base_config["staff_operations"] or (
        base_config["staff_operations"]["thresholds_by_zone"] == {}
    )


def test_per_zone_staff_task_thresholds_reject_unknown_zones_and_fields() -> None:
    builder = InitialScenarioBuilder()
    with pytest.raises(ValueError, match="unknown zone IDs"):
        builder.build(_override_request(model_parameter_overrides={
            "staff_operations.thresholds_by_zone.*": {
                "missing-zone": {"patrol_stress_at_least": 0.5},
            },
        }))
    with pytest.raises(ValueError, match="unsupported per-zone fields"):
        builder.build(_override_request(model_parameter_overrides={
            "staff_operations.thresholds_by_zone.*": {
                "sleeping": {"unknown_threshold": 0.5},
            },
        }))


def test_staff_task_kind_durations_are_provenanced_and_checkpointed() -> None:
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request())
    customized = builder.build(_override_request(model_parameter_overrides={
        "staff_operations.task_duration_minutes.*": {
            "cleaning": 37,
            "patrol": 45,
        },
    }))
    baseline_config = baseline.scenario.model_configs["staff_operations"]
    config = customized.scenario.model_configs["staff_operations"]
    assert config["task_duration_minutes"] == {
        **baseline_config["task_duration_minutes"],
        "cleaning": 37,
        "patrol": 45,
    }

    base_world = WorldState.from_scenario(baseline.scenario)
    customized_world = WorldState.from_scenario(customized.scenario)
    before = Checkpoint.capture(base_world)
    after = Checkpoint.capture(customized_world)
    assert before.model_versions["policy:staff_operations"] != (
        after.model_versions["policy:staff_operations"]
    )
    assert Checkpoint.from_json(after.to_json()).restore_world().canonical_json() == (
        customized_world.canonical_json()
    )
    provenance = [
        row for row in customized.scenario.provenance["assumptions"]
        if row.get("subject") == "staff_operations.task_duration_minutes.*"
    ]
    assert len(provenance) == 1
    assert provenance[0]["scope"] == "per_task_kind"
    assert provenance[0]["value"] == {"cleaning": 37, "patrol": 45}
    catalog_entry = next(
        entry["parameters"]["staff_operations.task_duration_minutes.*"]
        for entry in model_catalog().values()
        if "staff_operations.task_duration_minutes.*" in entry["parameters"]
    )
    assert catalog_entry["scope"] == "per_task_kind"

    with pytest.raises(ValueError, match="unknown task kind"):
        builder.build(_override_request(model_parameter_overrides={
            "staff_operations.task_duration_minutes.*": {"unknown_task": 10},
        }))
    with pytest.raises(ValueError, match="integer task-kind minutes"):
        builder.build(_override_request(model_parameter_overrides={
            "staff_operations.task_duration_minutes.*": {"cleaning": 2.5},
        }))
    with pytest.raises(ValueError, match="outside its Model Catalog bounds"):
        builder.build(_override_request(model_parameter_overrides={
            "staff_operations.task_duration_minutes.*": {"cleaning": 1441},
        }))


def test_per_zone_staff_effect_coefficients_are_consumed_signed_and_provenanced() -> None:
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request())
    customized = builder.build(_override_request(model_parameter_overrides={
        "staff_operations.effects_by_zone.*": {
            "sleeping": {
                "cleaning_sanitation_recovery": 0.32,
                "outreach_uncertainty_relief": 0.08,
            },
        },
    }))
    config = customized.scenario.model_configs["staff_operations"]
    assert config["effects_by_zone"]["sleeping"] == {
        "cleaning_sanitation_recovery": 0.32,
        "outreach_uncertainty_relief": 0.08,
    }

    baseline_world = WorldState.from_scenario(baseline.scenario)
    customized_world = WorldState.from_scenario(customized.scenario)
    before = Checkpoint.capture(baseline_world)
    after = Checkpoint.capture(customized_world)
    assert before.model_versions["policy:staff_operations"] != (
        after.model_versions["policy:staff_operations"]
    )
    assert Checkpoint.from_json(after.to_json()).restore_world().canonical_json() == (
        customized_world.canonical_json()
    )
    provenance = [
        row for row in customized.scenario.provenance["assumptions"]
        if row.get("subject") == "staff_operations.effects_by_zone.*"
    ]
    assert len(provenance) == 1
    assert provenance[0]["scope"] == "per_zone_id"
    assert provenance[0]["value"] == config["effects_by_zone"]
    assert provenance[0]["field_units"]["cleaning_sanitation_recovery"] == (
        "normalized_sanitation_index_per_cleaning_task"
    )

    with pytest.raises(ValueError, match="unknown zone IDs"):
        builder.build(_override_request(model_parameter_overrides={
            "staff_operations.effects_by_zone.*": {
                "unknown-zone": {"cleaning_sanitation_recovery": 0.3},
            },
        }))
    with pytest.raises(ValueError, match="unsupported per-zone fields"):
        builder.build(_override_request(model_parameter_overrides={
            "staff_operations.effects_by_zone.*": {
                "sleeping": {"unsupported_effect": 0.3},
            },
        }))
    with pytest.raises(ValueError, match="outside its Model Catalog bounds"):
        builder.build(_override_request(model_parameter_overrides={
            "staff_operations.effects_by_zone.*": {
                "sleeping": {"sanitation_use_per_person_minute": 0.02},
            },
        }))


def test_per_agent_sleep_debt_coefficient_changes_only_selected_agent_feedback() -> None:
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request())
    agent_id = baseline.scenario.profiles[0].id
    customized = builder.build(_override_request(model_parameter_overrides={
        "body_dynamics.sleep_debt_stress_rate_by_agent.*": {agent_id: 0.01},
    }))
    assert customized.scenario.model_configs["body_dynamics"][
        "sleep_debt_stress_rate_by_agent"
    ] == {agent_id: 0.01}

    baseline_world = WorldState.from_scenario(baseline.scenario)
    customized_world = WorldState.from_scenario(customized.scenario)
    for world in (baseline_world, customized_world):
        world.model_state["body_state_v1"]["agents"][agent_id][
            "sleep_debt_minutes"
        ] = 720.0

    baseline_result = create_psychology(baseline_world.model_configs).explain_update(
        WorldView.from_world(baseline_world), 0
    )
    customized_result = create_psychology(customized_world.model_configs).explain_update(
        WorldView.from_world(customized_world), 0
    )
    baseline_stress = {item.agent_id: item.stress for item in baseline_result.deltas}
    customized_stress = {item.agent_id: item.stress for item in customized_result.deltas}
    assert customized_stress[agent_id] > baseline_stress[agent_id]
    for other_id in baseline_stress.keys() - {agent_id}:
        assert customized_stress[other_id] == baseline_stress[other_id]

    before = Checkpoint.capture(baseline_world)
    after = Checkpoint.capture(customized_world)
    assert before.model_versions["policy:body_dynamics"].startswith(
        "body-feedback-complete-v2@1:state1:"
    )
    assert before.model_versions["policy:body_dynamics"] != (
        after.model_versions["policy:body_dynamics"]
    )
    assert Checkpoint.from_json(after.to_json()).restore_world().canonical_json() == (
        customized_world.canonical_json()
    )
    provenance = [
        row for row in customized.scenario.provenance["assumptions"]
        if row.get("subject") == "body_dynamics.sleep_debt_stress_rate_by_agent.*"
    ]
    assert len(provenance) == 1
    assert provenance[0]["value"] == {agent_id: 0.01}
    assert provenance[0]["scope"] == "per_agent_id"

    with pytest.raises(ValueError, match="unknown Agent IDs"):
        builder.build(_override_request(model_parameter_overrides={
            "body_dynamics.sleep_debt_stress_rate_by_agent.*": {"unknown-agent": 0.01},
        }))
    with pytest.raises(ValueError, match="outside its Model Catalog bounds"):
        builder.build(_override_request(model_parameter_overrides={
            "body_dynamics.sleep_debt_stress_rate_by_agent.*": {agent_id: 1.1},
        }))


def test_per_agent_psychology_need_rates_change_only_selected_resident() -> None:
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request())
    agent_id = baseline.scenario.profiles[0].id
    overrides = {
        "psychology.toilet_growth_by_agent.*": {agent_id: 0.02},
        "psychology.hunger_growth_by_agent.*": {agent_id: 0.02},
        "psychology.awake_fatigue_growth_by_agent.*": {agent_id: 0.03},
    }
    customized = builder.build(_override_request(model_parameter_overrides=overrides))
    baseline_config = baseline.scenario.model_configs["psychology"]
    config = customized.scenario.model_configs["psychology"]
    assert not {
        "toilet_growth_by_agent",
        "hunger_growth_by_agent",
        "awake_fatigue_growth_by_agent",
    } & baseline_config.keys()
    assert config["toilet_growth_by_agent"] == {agent_id: 0.02}
    assert config["hunger_growth_by_agent"] == {agent_id: 0.02}
    assert config["awake_fatigue_growth_by_agent"] == {agent_id: 0.03}

    baseline_world = WorldState.from_scenario(baseline.scenario)
    customized_world = WorldState.from_scenario(customized.scenario)
    for world in (baseline_world, customized_world):
        for agent in world.agents.values():
            agent.activity = Activity.IDLE
        agent = world.agents[agent_id]
        agent.needs.toilet_urgency = 0.1
        agent.needs.hunger = 0.2
        agent.needs.fatigue = 0.3
    for psychology_id in (
        "causal-stress-v1",
        "bounded-stress-v1",
        "life-state-feedback-v1",
        "mean-reverting-life-state-v1",
    ):
        baseline_configs = deepcopy(baseline_world.model_configs)
        customized_configs = deepcopy(customized_world.model_configs)
        for configs in (baseline_configs, customized_configs):
            configs["policy_registry"] = {
                "schema_version": 1,
                "psychology": psychology_id,
            }
        baseline_result = create_psychology(baseline_configs).explain_update(
            WorldView.from_world(baseline_world), 0
        )
        customized_result = create_psychology(customized_configs).explain_update(
            WorldView.from_world(customized_world), 0
        )
        base_by_id = {item.agent_id: item for item in baseline_result.deltas}
        changed_by_id = {item.agent_id: item for item in customized_result.deltas}
        assert changed_by_id[agent_id].toilet_urgency - base_by_id[agent_id].toilet_urgency == (
            pytest.approx(0.019)
        )
        assert changed_by_id[agent_id].hunger - base_by_id[agent_id].hunger == pytest.approx(
            0.0195
        )
        assert changed_by_id[agent_id].fatigue - base_by_id[agent_id].fatigue == pytest.approx(
            0.0292
        )
        for other_id in base_by_id.keys() - {agent_id}:
            assert changed_by_id[other_id] == base_by_id[other_id]

    before = Checkpoint.capture(baseline_world)
    after = Checkpoint.capture(customized_world)
    assert before.model_versions["policy:psychology"].startswith(
        "mean-reverting-life-state-v1@1:state1:"
    )
    assert before.model_versions["policy:psychology"] != after.model_versions[
        "policy:psychology"
    ]
    assert Checkpoint.from_json(after.to_json()).restore_world().canonical_json() == (
        customized_world.canonical_json()
    )
    provenance = {
        row["subject"]: row
        for row in customized.scenario.provenance["assumptions"]
        if row.get("subject") in overrides
    }
    assert {path: row["value"] for path, row in provenance.items()} == overrides
    assert all(
        row["scope"] == "per_agent_id"
        and row["evidence_grade"] == "C"
        and row["source_id"] == "ASM-LIFE-STATE-FEEDBACK-V1"
        for row in provenance.values()
    )

    with pytest.raises(ValueError, match="unknown Agent IDs"):
        builder.build(_override_request(model_parameter_overrides={
            "psychology.toilet_growth_by_agent.*": {"unknown-agent": 0.02},
        }))
    with pytest.raises(ValueError, match="outside its Model Catalog bounds"):
        builder.build(_override_request(model_parameter_overrides={
            "psychology.hunger_growth_by_agent.*": {agent_id: 0.3},
        }))
    invalid_scenario = baseline.scenario.model_dump(mode="json")
    invalid_scenario["model_configs"]["psychology"]["toilet_growth_by_agent"] = {
        "unknown-agent": 0.02,
    }
    with pytest.raises(ValueError, match="unknown Agent IDs"):
        ScenarioSpec.model_validate(invalid_scenario)


@pytest.mark.parametrize(
    ("coefficient_path", "body_field"),
    [
        ("body_dynamics.negative_affect_stress_rate_by_agent.*", "uncertainty"),
        ("body_dynamics.discontent_stress_rate_by_agent.*", "discontent"),
    ],
)
def test_per_agent_body_feedback_rates_change_only_selected_resident(
    coefficient_path: str, body_field: str
) -> None:
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request())
    agent_id = baseline.scenario.profiles[0].id
    customized = builder.build(_override_request(model_parameter_overrides={
        coefficient_path: {agent_id: 0.2},
    }))
    config_field = coefficient_path.rsplit(".", 2)[1].removesuffix(".*")
    assert customized.scenario.model_configs["body_dynamics"][config_field] == {
        agent_id: 0.2
    }

    baseline_world = WorldState.from_scenario(baseline.scenario)
    customized_world = WorldState.from_scenario(customized.scenario)
    for world in (baseline_world, customized_world):
        world.model_state["body_state_v1"]["agents"][agent_id][body_field] = 1.0
    baseline_result = create_psychology(baseline_world.model_configs).explain_update(
        WorldView.from_world(baseline_world), 0
    )
    customized_result = create_psychology(customized_world.model_configs).explain_update(
        WorldView.from_world(customized_world), 0
    )
    baseline_stress = {item.agent_id: item.stress for item in baseline_result.deltas}
    customized_stress = {item.agent_id: item.stress for item in customized_result.deltas}
    assert customized_stress[agent_id] > baseline_stress[agent_id]
    for other_id in baseline_stress.keys() - {agent_id}:
        assert customized_stress[other_id] == baseline_stress[other_id]

    before = Checkpoint.capture(baseline_world)
    after = Checkpoint.capture(customized_world)
    assert before.model_versions["policy:body_dynamics"] != (
        after.model_versions["policy:body_dynamics"]
    )
    assert Checkpoint.from_json(after.to_json()).restore_world().canonical_json() == (
        customized_world.canonical_json()
    )
    provenance = [
        row for row in customized.scenario.provenance["assumptions"]
        if row.get("subject") == coefficient_path
    ]
    assert len(provenance) == 1
    assert provenance[0]["value"] == {agent_id: 0.2}


def test_per_resident_care_response_overrides_are_signed_and_checkpointed() -> None:
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request(
        care_outcome_policy_id="hygiene-support-proportional-relief-v1",
    ))
    agent_id = baseline.scenario.profiles[0].id
    customized = builder.build(_override_request(
        care_outcome_policy_id="hygiene-support-proportional-relief-v1",
        model_parameter_overrides={
            "body_dynamics.complaint_reassurance_gain_by_agent.*": {agent_id: 0.04},
            "body_dynamics.hygiene_support_relief_by_agent.*": {agent_id: 0.3},
        },
    ))
    config = customized.scenario.model_configs["body_dynamics"]
    assert config["complaint_reassurance_gain_by_agent"] == {agent_id: 0.04}
    assert config["hygiene_support_relief_by_agent"] == {agent_id: 0.3}

    baseline_signatures = model_signatures(baseline.scenario.model_configs)
    customized_signatures = model_signatures(customized.scenario.model_configs)
    assert baseline_signatures["policy:body_dynamics"] != customized_signatures[
        "policy:body_dynamics"
    ]
    assert baseline_signatures["policy:care_outcome"] != customized_signatures[
        "policy:care_outcome"
    ]
    world = WorldState.from_scenario(customized.scenario)
    checkpoint = Checkpoint.capture(world)
    assert Checkpoint.from_json(checkpoint.to_json()).restore_world().canonical_json() == (
        world.canonical_json()
    )
    assumptions = {
        row["subject"]: row for row in customized.scenario.provenance["assumptions"]
    }
    assert assumptions[
        "body_dynamics.complaint_reassurance_gain_by_agent.*"
    ]["value"] == {agent_id: 0.04}
    assert assumptions[
        "body_dynamics.hygiene_support_relief_by_agent.*"
    ]["value"] == {agent_id: 0.3}

    with pytest.raises(ValueError, match="unknown Agent IDs"):
        builder.build(_override_request(
            care_outcome_policy_id="hygiene-support-proportional-relief-v1",
            model_parameter_overrides={
                "body_dynamics.hygiene_support_relief_by_agent.*": {
                    "unknown-agent": 0.3,
                },
            },
        ))


def test_typed_care_response_overrides_reach_formula_and_checkpoint() -> None:
    builder = InitialScenarioBuilder()
    baseline = builder.build(_override_request())
    agent_id = baseline.scenario.profiles[0].id
    customized = builder.build(_override_request(model_parameter_overrides={
        "body_dynamics.privacy_accommodation_discontent_relief_by_agent.*": {
            agent_id: 0.07,
        },
    }))
    config = customized.scenario.model_configs
    formula = create_care_outcome_formula(config)
    assert formula is not None
    assert formula.support_state_deltas(
        "privacy_accommodation", agent_id=agent_id
    ) == {"body_state_v1.agents.discontent": -0.07}
    assert model_signatures(baseline.scenario.model_configs)["policy:care_outcome"] != (
        model_signatures(config)["policy:care_outcome"]
    )
    world = WorldState.from_scenario(customized.scenario)
    checkpoint = Checkpoint.capture(world)
    assert Checkpoint.from_json(checkpoint.to_json()).restore_world().canonical_json() == (
        world.canonical_json()
    )
    assumption = next(
        item for item in customized.scenario.provenance["assumptions"]
        if item.get("subject")
        == "body_dynamics.privacy_accommodation_discontent_relief_by_agent.*"
    )
    assert assumption["value"] == {agent_id: 0.07}
    assert assumption["scope"] == "per_agent_id"

    with pytest.raises(ValueError, match="unknown Agent IDs"):
        builder.build(_override_request(model_parameter_overrides={
            "body_dynamics.privacy_accommodation_discontent_relief_by_agent.*": {
                "unknown-agent": 0.07,
            },
        }))


def test_strict_fifo_rejects_unused_queue_coefficients_without_invalidating_checkpoints() -> None:
    builder = InitialScenarioBuilder()
    scenario = builder.build(_override_request(queue_policy_id="strict-fifo-queue-v1")).scenario
    world = WorldState.from_scenario(scenario)
    checkpoint = Checkpoint.capture(world)

    assert model_catalog()["strict-fifo-queue-v1"]["parameters"] == {}
    assert checkpoint.model_versions["policy:queue"] == (
        "strict-fifo-queue-v1@1:state1:manifest="
        "ab880f1d08db9e603f155b09297214403dac87e43fd0d955a79c170c6c550b5c:inputs="
        "4391e25bc21a325164c4895a28ea674bf8be49724fe4f04dbe9657876ea2ff12"
    )
    assert Checkpoint.from_json(checkpoint.to_json()).restore_world().canonical_json() == (
        world.canonical_json()
    )
    for path, value in (
        ("facility_queues.retry_delay_minutes", 1),
        ("facility_queues.persistence_urgency_threshold", 0.5),
    ):
        with pytest.raises(ValueError, match="not consumed by a selected installed formula"):
            builder.build(_override_request(
                queue_policy_id="strict-fifo-queue-v1",
                model_parameter_overrides={path: value},
            ))


def test_per_staff_shift_windows_flow_through_scenario_evaluation_and_checkpoint() -> None:
    path = "staff_dynamics.shift_windows_by_staff.*"
    builder = InitialScenarioBuilder()
    base = builder.build(_override_request(staff_count=2)).scenario
    staff_ids = [staff.id for staff in base.staff]
    assert len(staff_ids) == 2

    scenario = builder.build(_override_request(
        staff_count=2,
        operational_conditions=OperationalConditions(staff_shifts=(
            StaffShiftInput(
                staff_id=staff_ids[0],
                start_minute=60,
                end_minute=300,
            ),
        )),
        model_parameter_overrides={
            path: {
                staff_ids[1]: [{"start_minute": 480, "end_minute": 960}],
            },
        },
    )).scenario
    config = scenario.model_configs["staff_dynamics"]
    assert config["policy_version"] == "continuous-duty-fatigue-v2"
    assert config["shift_windows_by_staff"][staff_ids[0]] == [
        {"start_minute": 60, "end_minute": 300},
    ]
    parameter = next(row for row in parameter_catalog(scenario) if row["path"] == path)
    assert parameter["scope"] == "per_staff_shift_windows"
    assert parameter["value"] == config["shift_windows_by_staff"]

    candidate_windows = {
        staff_ids[0]: [{"start_minute": 0, "end_minute": 360}],
        staff_ids[1]: [{"start_minute": 360, "end_minute": 720}],
    }
    candidate = _variant_scenario(
        scenario,
        EvaluationVariant.model_validate({
            "id": "earlier-shift-rotation",
            "parameters": {path: candidate_windows},
        }),
        seed=8,
    )
    assert candidate.model_configs["staff_dynamics"]["shift_windows_by_staff"] == candidate_windows
    assert model_signatures(candidate.model_configs) != model_signatures(scenario.model_configs)
    assumption = next(
        row for row in candidate.provenance["assumptions"] if row.get("path") == path
    )
    assert assumption["value"] == candidate_windows
    assert assumption["scope"] == "per_staff_shift_windows"

    world = WorldState.from_scenario(candidate)
    checkpoint = Checkpoint.capture(world)
    restored = checkpoint.restore_world()
    assert restored.canonical_json() == world.canonical_json()
    assert restored.model_configs["staff_dynamics"]["shift_windows_by_staff"] == candidate_windows
    assert staff_dynamics_on_shift(restored, staff_ids[0], 0)
    assert not staff_dynamics_on_shift(restored, staff_ids[1], 0)
    assert staff_dynamics_on_shift(restored, staff_ids[1], 360)

    with pytest.raises(ValueError, match="unknown staff IDs"):
        _variant_scenario(
            scenario,
            EvaluationVariant.model_validate({
                "id": "unknown-shift-owner",
                "parameters": {path: {"S-UNKNOWN": [{"start_minute": 0, "end_minute": 60}]}},
            }),
            seed=9,
        )
    with pytest.raises(ValueError, match="cannot overlap"):
        EvaluationVariant.model_validate({
            "id": "overlapping-shifts",
            "parameters": {
                path: {staff_ids[0]: [
                    {"start_minute": 0, "end_minute": 60},
                    {"start_minute": 50, "end_minute": 80},
                ]},
            },
        })

