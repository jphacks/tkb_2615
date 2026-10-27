"""Opt-in zone environment effects, provenance, replay, and compiler coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.core.checkpoint import Checkpoint, CheckpointError
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.environment import (
    ZONE_ENVIRONMENT_POLICY,
    ZONE_ENVIRONMENT_STATE_KEY,
    DemandRateParameters,
    ScenarioZoneEnvironmentConfig,
    zone_environment_view,
)
from sheltersim.events.effects import (
    DEFAULT_PRIMITIVE_REGISTRY,
    EffectProgram,
    EffectProgramEnvironmentHandler,
    EffectProgramValidationError,
    SupportMode,
    TargetSelector,
    TimedAction,
    schedule_effect_program,
    validate_effect_program,
)
from sheltersim.events.handlers import EventDispatcher
from sheltersim.events.models import EventStatus
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import _model_assumptions
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.contracts import ProviderResponse
from sheltersim.llm.natural_language import (
    _eligible_primitive_specs,
    _target_ids_by_kind,
    compile_natural_language,
    scenario_summary_from_world,
)
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.schemas import ParseRequest
from sheltersim.policies.behavior import ActionKind, UtilityBehaviorPolicy, WorldView
from sheltersim.policies.psychology import Activity, SimplePsychologyPolicy

ROOT = Path(__file__).parents[3]
SMALL_SCENARIO = ROOT / "fixtures" / "scenarios" / "h00_small_replay.json"
ENVIRONMENT_SOURCE = "ASM-H19-ZONE-ENVIRONMENT-V1"


def _world(
    *,
    environment: bool = True,
    max_rest_minutes: int = 60,
    temperature_model: str | None = None,
    formula_models: dict[str, str] | None = None,
) -> WorldState:
    document = json.loads(SMALL_SCENARIO.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["scheduled_events"] = []
    if environment:
        environment_values = {"max_rest_decision_minutes": max_rest_minutes}
        if temperature_model is not None or formula_models:
            environment_values["policy_version"] = "zone-environment-v2"
        document["model_configs"]["zone_environment"] = (
            ScenarioZoneEnvironmentConfig.model_validate(environment_values).model_dump(mode="json")
        )
    selections = dict(formula_models or {})
    if temperature_model is not None:
        selections["zone_environment"] = temperature_model
    if selections:
        document["model_configs"]["policy_registry"] = {
            "schema_version": 2,
            **selections,
        }
    return WorldState.from_scenario(ScenarioSpec.model_validate(document))


def _effect(
    effect_id: str,
    op: str,
    parameters: dict[str, object],
    *,
    start: int = 1,
    end: int = 10,
) -> TimedAction:
    return TimedAction(
        effect_id=effect_id,
        op=op,
        target_selector=TargetSelector(kind="zone", target_id="sleeping"),
        start_minute=start,
        end_minute=end,
        parameters=parameters,
        units={
            "set_ambient_noise": "normalized_noise_0_1",
            "set_lighting": "normalized_lighting_0_1",
            "set_privacy": "normalized_privacy_0_1",
            "set_ventilation": "relative_ventilation_effectiveness_0_1",
        }.get(op, "relative_action_rate"),
        source_id="ASM-H19-TEST-EFFECT-V1",
        assumption_ids=[],
        policy_dependencies=[ZONE_ENVIRONMENT_POLICY],
    )


def _program(*effects: TimedAction, program_id: str = "h19-test-program") -> EffectProgram:
    return EffectProgram(
        program_id=program_id,
        narrative="A bounded zone environment test.",
        source_text="test input",
        simulation_start_minute=min(item.start_minute for item in effects),
        effects=list(effects),
        support_mode=SupportMode.COMPOSED if len(effects) > 1 else SupportMode.NATIVE,
        compiler_provenance={
            "compiler_kind": "offline_rule",
            "compiler_version": "h19-test-v1",
        },
    )


def _dispatcher() -> EventDispatcher:
    return EventDispatcher(
        {
            "effect_program_environment": EffectProgramEnvironmentHandler(),
            "effect_program_environment_expire": EffectProgramEnvironmentHandler(expire=True),
        }
    )


def _activate(world: WorldState, *effects: TimedAction) -> tuple[str, ...]:
    program = _program(*effects)
    scheduled = schedule_effect_program(world, program)
    world.minute_index = effects[0].start_minute
    records = _dispatcher().dispatch_due(world, world.minute_index)
    assert records and all(record.status is EventStatus.APPLIED for record in records)
    return scheduled.event_ids


def test_noise_changes_rest_recovery_only_in_the_target_zone_and_never_writes_stress() -> None:
    baseline = _world(environment=False)
    noisy = _world()
    for world in (baseline, noisy):
        for agent in world.agents.values():
            agent.activity = Activity.RESTING
            agent.needs.toilet_urgency = 0.1
            agent.needs.hunger = 0.1
            agent.needs.fatigue = 0.6
            agent.stress = 0.1
        world.agents["A004"].zone_id = "corridor"
        world.minute_index = 1

    event_ids = _activate(
        noisy,
        _effect("night-noise", "set_ambient_noise", {"level": 0.8}),
    )
    assert zone_environment_view(noisy, "sleeping").rest_efficiency_multiplier == 0.6
    assert zone_environment_view(noisy, "corridor").rest_efficiency_multiplier == 1.0
    base_result = SimplePsychologyPolicy().explain_update(WorldView.from_world(baseline), 1)
    noise_result = SimplePsychologyPolicy().explain_update(WorldView.from_world(noisy), 1)
    base_deltas = {item.agent_id: item for item in base_result.deltas}
    noise_deltas = {item.agent_id: item for item in noise_result.deltas}
    base_traces = {item.agent_id: item for item in base_result.traces}
    noise_traces = {item.agent_id: item for item in noise_result.traces}

    assert noise_deltas["A001"].fatigue > base_deltas["A001"].fatigue
    assert noise_deltas["A004"].fatigue == base_deltas["A004"].fatigue
    assert noise_deltas["A001"].stress == base_deltas["A001"].stress
    assert noise_deltas["A004"].stress == base_deltas["A004"].stress
    assert noise_traces["A001"].zone_environment_modifier_event_ids == (event_ids[0],)
    assert noise_traces["A001"].to_dict(source_id="ASM-H04-CAUSAL-STRESS-V1")["zone_environment"][
        "assumption_ids"
    ] == [ENVIRONMENT_SOURCE]
    assert noise_traces["A004"].zone_environment_modifier_event_ids == ()
    assert base_traces["A001"].zone_environment_modifier_event_ids == ()


def test_demand_odds_are_action_and_zone_scoped_and_rest_duration_is_clamped() -> None:
    baseline = _world(environment=False)
    modified = _world(max_rest_minutes=20)
    for world in (baseline, modified):
        for agent in world.agents.values():
            agent.activity = Activity.IDLE
            agent.next_decision_at = 0
            agent.needs.toilet_urgency = 0.5
            agent.needs.hunger = 0.1
            agent.needs.fatigue = 0.8
            agent.stress = 0.1
        world.agents["A004"].zone_id = "corridor"
        world.minute_index = 1

    event_ids = _activate(
        modified,
        _effect(
            "sleep-demand",
            "modify_demand_rate",
            {
                "action": "sleep",
                "probability_multiplier": 2.0,
                "duration_multiplier": 2.0,
            },
        ),
    )
    policy = UtilityBehaviorPolicy(decision_interval_minutes=15)
    baseline_view = WorldView.from_world(baseline)
    modified_view = WorldView.from_world(modified)
    base_target = policy.propose(baseline_view, "A001", CounterRNG(3))
    modified_target = policy.propose(modified_view, "A001", CounterRNG(3))
    base_other = policy.propose(baseline_view, "A004", CounterRNG(3))
    modified_other = policy.propose(modified_view, "A004", CounterRNG(3))
    base_target_sleep = next(
        item for item in base_target.candidates if item.action is ActionKind.SLEEP
    )
    modified_target_sleep = next(
        item for item in modified_target.candidates if item.action is ActionKind.SLEEP
    )
    base_other_sleep = next(
        item for item in base_other.candidates if item.action is ActionKind.SLEEP
    )
    modified_other_sleep = next(
        item for item in modified_other.candidates if item.action is ActionKind.SLEEP
    )

    assert modified_target_sleep.probability > base_target_sleep.probability
    assert modified_other_sleep.probability == base_other_sleep.probability
    assert "experimental zone action multiplier x2" in modified_target_sleep.trigger
    assert "decision interval x2" in modified_target_sleep.trigger
    assert f"events={event_ids[0]}" in modified_target_sleep.trigger
    assert f"assumptions={ENVIRONMENT_SOURCE}" in modified_target_sleep.trigger
    modified_target_rest = next(
        item for item in modified_target.candidates if item.action is ActionKind.REST
    )
    assert "experimental zone action multiplier" not in modified_target_rest.trigger
    selected_sleep = next(
        proposal
        for seed in range(100)
        if (proposal := policy.propose(modified_view, "A001", CounterRNG(seed))).action
        is ActionKind.SLEEP
    )
    assert selected_sleep.next_decision_at == 21


def test_environment_modifiers_expire_and_checkpoint_replay_is_identical() -> None:
    world = _world()
    event_ids = _activate(
        world,
        _effect("night-noise", "set_ambient_noise", {"level": 0.8}, end=5),
    )
    active_view = zone_environment_view(world, "sleeping")
    assert active_view.ambient_noise_level == 0.8
    assert active_view.noise_modifier_event_ids == (event_ids[0],)

    checkpoint = Checkpoint.from_json(Checkpoint.capture(world).to_json())
    assert "zone_environment" in checkpoint.model_versions
    restored = checkpoint.restore_world()
    assert (
        restored.model_state[ZONE_ENVIRONMENT_STATE_KEY]
        == world.model_state[ZONE_ENVIRONMENT_STATE_KEY]
    )

    outcomes = []
    for candidate in (world, restored):
        candidate.minute_index = 5
        outcomes.append(_dispatcher().dispatch_due(candidate, 5))
    assert all(records[0].status is EventStatus.APPLIED for records in outcomes)
    assert world.model_state[ZONE_ENVIRONMENT_STATE_KEY]["modifiers"] == {}
    assert world.canonical_json() == restored.canonical_json()


def test_environment_formula_factory_changes_temperature_response_and_signature() -> None:
    comfort_band = _world(temperature_model="zone-temperature-comfort-band-v1")
    midpoint = _world(temperature_model="zone-temperature-midpoint-v1")

    comfort_response = zone_environment_view(comfort_band, "sleeping")
    midpoint_response = zone_environment_view(midpoint, "sleeping")

    assert comfort_response.temperature_stress_per_minute == 0.0
    assert midpoint_response.temperature_stress_per_minute == pytest.approx(0.0002)
    comfort_checkpoint = Checkpoint.capture(comfort_band)
    midpoint_checkpoint = Checkpoint.capture(midpoint)
    assert (
        comfort_checkpoint.model_versions["policy:zone_environment"]
        != midpoint_checkpoint.model_versions["policy:zone_environment"]
    )
    with pytest.raises(CheckpointError, match="do not match"):
        comfort_checkpoint.restore_world(expected_model_versions=midpoint_checkpoint.model_versions)


def test_environment_factories_independently_change_rest_hygiene_and_privacy() -> None:
    rest_multiplicative = _world(formula_models={"zone_rest": "zone-rest-multiplicative-v1"})
    rest_limiting = _world(formula_models={"zone_rest": "zone-rest-limiting-v1"})
    for world in (rest_multiplicative, rest_limiting):
        _activate(
            world,
            _effect("night-noise", "set_ambient_noise", {"level": 0.8}),
            _effect("night-light", "set_lighting", {"level": 0.8}),
        )

    assert zone_environment_view(rest_multiplicative, "sleeping").rest_efficiency_multiplier == (
        pytest.approx(0.504)
    )
    assert zone_environment_view(rest_limiting, "sleeping").rest_efficiency_multiplier == (
        pytest.approx(0.6)
    )

    linear = _world(formula_models={"zone_hygiene": "zone-hygiene-linear-v1"})
    quadratic = _world(formula_models={"zone_hygiene": "zone-hygiene-quadratic-v1"})
    threshold = _world(formula_models={"zone_privacy": "zone-privacy-threshold-v1"})
    proportional = _world(formula_models={"zone_privacy": "zone-privacy-proportional-v1"})
    effects = (
        _effect("low-ventilation", "set_ventilation", {"effectiveness": 0.3}),
        _effect("low-privacy", "set_privacy", {"level": 0.2}),
    )
    for world in (linear, quadratic, threshold, proportional):
        _activate(world, *effects)

    assert zone_environment_view(linear, "sleeping").ventilation_hygiene_growth_per_minute == (
        pytest.approx(0.00007)
    )
    assert zone_environment_view(quadratic, "sleeping").ventilation_hygiene_growth_per_minute == (
        pytest.approx(0.000049)
    )
    assert zone_environment_view(threshold, "sleeping").privacy_discontent_per_minute == (
        pytest.approx(0.00002)
    )
    assert zone_environment_view(proportional, "sleeping").privacy_discontent_per_minute == (
        pytest.approx(0.00008)
    )


def test_invalid_environment_programs_fail_preflight_without_world_mutation() -> None:
    world = _world()
    before = world.canonical_json()
    no_expiry = _effect("no-expiry", "set_ambient_noise", {"level": 0.5}, end=10)
    no_expiry = no_expiry.model_copy(update={"end_minute": None})
    unknown_zone = _effect("unknown-zone", "set_ambient_noise", {"level": 0.5})
    unknown_zone = unknown_zone.model_copy(
        update={"target_selector": TargetSelector(kind="zone", target_id="missing")}
    )
    bad_demand = _effect(
        "bad-duration",
        "modify_demand_rate",
        {"action": "seek_help", "probability_multiplier": 1.5, "duration_multiplier": 1.5},
    )

    assert any(
        "requires end_minute" in issue
        for issue in validate_effect_program(world, _program(no_expiry))
    )
    assert any(
        "unknown zone" in issue for issue in validate_effect_program(world, _program(unknown_zone))
    )
    with pytest.raises(ValueError, match="only for rest and sleep"):
        DemandRateParameters.model_validate(bad_demand.parameters)
    assert world.canonical_json() == before
    with pytest.raises(EffectProgramValidationError):
        schedule_effect_program(world, _program(no_expiry))
    assert world.canonical_json() == before


def test_environment_coefficients_are_editable_and_written_as_assumptions() -> None:
    document = json.loads(SMALL_SCENARIO.read_text(encoding="utf-8"))
    config = ScenarioZoneEnvironmentConfig(noise_rest_efficiency_loss=0.8)
    document["schema_version"] = 3
    document["model_configs"]["zone_environment"] = config.model_dump(mode="json")
    scenario = ScenarioSpec.model_validate(document)

    assumptions = _model_assumptions(scenario, execution_mode="offline")

    assert scenario.model_configs["zone_environment"]["noise_rest_efficiency_loss"] == 0.8
    assert assumptions["zone_environment"]["source_type"] == "assumption"
    assert assumptions["zone_environment"]["source_id"] == ENVIRONMENT_SOURCE
    assert assumptions["zone_environment"]["values"]["noise_rest_efficiency_loss"] == 0.8
    assert (
        "Neither is a physical, medical, or crowd prediction"
        in assumptions["zone_environment"]["claim_limit"]
    )


class _RecordedCompilerProvider:
    def __init__(self, text: str) -> None:
        self.text = text

    def complete(
        self,
        *,
        model_id: str,
        task: object,
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ProviderResponse:
        del task, max_output_tokens, timeout_seconds
        return ProviderResponse(
            text=self.text,
            model_id=model_id,
            input_tokens=100,
            output_tokens=100,
            elapsed_ms=1.0,
        )


H19_COMPILER_CASES = json.loads(
    (ROOT / "fixtures" / "evaluation" / "h19_unseen_environment_cases.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("case", H19_COMPILER_CASES["cases"], ids=lambda item: item["id"])
def test_unseen_stories_compile_to_the_same_registered_environment_operators(
    case: dict[str, object],
) -> None:
    disabled_summary = scenario_summary_from_world(_world(environment=False))
    disabled_operators = {
        item.op
        for item in _eligible_primitive_specs(
            disabled_summary,
            _target_ids_by_kind(disabled_summary),
            DEFAULT_PRIMITIVE_REGISTRY,
        )
    }
    assert not {"set_ambient_noise", "modify_demand_rate"} & disabled_operators

    world = _world()
    summary = scenario_summary_from_world(world)
    eligible = {
        item.op
        for item in _eligible_primitive_specs(
            summary, _target_ids_by_kind(summary), DEFAULT_PRIMITIVE_REGISTRY
        )
    }
    assert {"set_ambient_noise", "modify_demand_rate"} <= eligible

    response = json.dumps(case["compiler_response"], ensure_ascii=False, separators=(",", ":"))
    router = LLMRouter(
        LLMSettings(mode=LLMMode.RECORD, budget_usd=1.0),
        provider=_RecordedCompilerProvider(response),
    )
    result = compile_natural_language(
        ParseRequest(
            text=case["story"],
            branch_id="baseline",
            selected_minute=0,
            scenario_summary=summary,
        ),
        router,
    )

    assert result.status == "compiled"
    assert result.draft_plan is not None
    assert {item.op for item in result.draft_plan.effects} == set(case["expected_operators"])
    assert all(item.target_selector.target_id == "sleeping" for item in result.draft_plan.effects)
