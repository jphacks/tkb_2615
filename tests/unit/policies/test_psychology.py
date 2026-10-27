"""Psychology policies: sleep debt, mean-reverting distress, latent distress and the
integrated life-state policy, with their traces.

The research social operator is checked in tests/unit/policies/test_social.py, and
full Runs with these policies in tests/integration/test_research_psychology.py.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from sheltersim.core.world import WorldState
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
from sheltersim.domain.models import (
    Activity,
    AgentInformationState,
    InformationBeliefStatus,
    InformationSpreadConfig,
    InformationTruthStatus,
    SupportRequestState,
    TypedMessage,
)
from sheltersim.domain.psychological_state import PSYCHOLOGICAL_STATE_KEY
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.behavior import WorldView
from sheltersim.policies.psychology import (
    IntegratedDistressLifeStatePolicy,
    MeanRevertingDistressPolicy,
    mean_reverting_step,
    sleep_debt_step,
)
from sheltersim.policies.registry import create_psychology

ROOT = Path(__file__).parents[3]
FIXTURE = ROOT / "fixtures" / "scenario_100_agents.json"


@pytest.mark.parametrize("required_sleep_minutes", [420, 480, 600])
def test_sleep_debt_balances_a_repeating_required_sleep_schedule(
    required_sleep_minutes: int,
) -> None:
    awake_minutes = 1440 - required_sleep_minutes
    awake_rate = required_sleep_minutes / awake_minutes
    debt = 0.0
    for _ in range(awake_minutes):
        debt = sleep_debt_step(
            debt,
            activity=Activity.IDLE,
            required_sleep_minutes=required_sleep_minutes,
            awake_rate=0.5,
            rest_recovery_rate=0.0,
            sleep_recovery_rate=1.0,
            sleep_efficiency=1.0,
        )
    assert debt == pytest.approx(required_sleep_minutes)
    for _ in range(required_sleep_minutes):
        debt = sleep_debt_step(
            debt,
            activity=Activity.SLEEPING,
            required_sleep_minutes=required_sleep_minutes,
            awake_rate=0.5,
            rest_recovery_rate=0.0,
            sleep_recovery_rate=1.0,
            sleep_efficiency=1.0,
        )
    assert debt == pytest.approx(0.0, abs=1e-9)
    assert awake_rate * awake_minutes == pytest.approx(required_sleep_minutes)


def test_sleep_debt_rest_and_unspecified_requirement_are_explicit_assumptions() -> None:
    resting = sleep_debt_step(
        120.0,
        activity=Activity.RESTING,
        required_sleep_minutes=None,
        awake_rate=0.5,
        rest_recovery_rate=0.0,
        sleep_recovery_rate=1.0,
        sleep_efficiency=1.0,
    )
    awake = sleep_debt_step(
        120.0,
        activity=Activity.IDLE,
        required_sleep_minutes=None,
        awake_rate=0.5,
        rest_recovery_rate=0.0,
        sleep_recovery_rate=1.0,
        sleep_efficiency=1.0,
    )
    assert resting == 120.0
    assert awake == 120.5
    with pytest.raises(ValueError, match="sleep_efficiency"):
        sleep_debt_step(
            120.0,
            activity=Activity.SLEEPING,
            required_sleep_minutes=480,
            awake_rate=0.5,
            rest_recovery_rate=0.0,
            sleep_recovery_rate=1.0,
            sleep_efficiency=float("nan"),
        )


def test_acute_distress_is_a_compatibility_alias_and_recovers_at_restless_ticks(
    research_psychology_scenario
) -> None:
    world = WorldState.from_scenario(research_psychology_scenario())
    agent = world.agents["A001"]
    agent.stress = 0.8
    assert agent.acute_distress == agent.stress
    agent.acute_distress = 0.6
    assert agent.stress == 0.6

    view = WorldView.from_world(world)
    result = create_psychology(world.model_configs).explain_update(view, 0)
    first = next(delta for delta in result.deltas if delta.agent_id == agent.id)
    assert first.stress == pytest.approx(0.6 * math.exp(-0.02))
    assert result.policy_version == "mean-reverting-distress-v1"


def test_exact_mean_reversion_matches_finer_time_steps_and_has_a_bounded_equilibrium() -> None:
    coarse = mean_reverting_step(0.2, 0.1, 0.005, 0.05, 1.0)
    fine = 0.2
    for _ in range(10):
        fine = mean_reverting_step(fine, 0.1, 0.005, 0.05, 0.1)
    assert coarse == pytest.approx(fine, abs=1e-12)

    sustained = 0.1
    for _ in range(2000):
        sustained = mean_reverting_step(sustained, 0.1, 0.005, 0.05, 1.0)
    assert sustained == pytest.approx(0.2, abs=1e-9)
    assert 0.0 <= sustained <= 1.0

    same_shock = [0.8] * 12
    for _ in range(300):
        same_shock = [mean_reverting_step(value, 0.0, 0.0, 0.01, 1.0) for value in same_shock]
    assert max(same_shock) < 0.05


def test_recovery_rate_changes_decay_speed_and_nonfinite_values_are_rejected() -> None:
    slow = mean_reverting_step(0.9, 0.0, 0.0, 0.005, 60.0)
    fast = mean_reverting_step(0.9, 0.0, 0.0, 0.05, 60.0)
    assert fast < slow
    with pytest.raises(ValueError, match="mean-reversion"):
        mean_reverting_step(0.5, 0.0, math.inf, 0.1, 1.0)


def test_personal_recovery_rates_allow_individual_decay_speeds() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["profiles"] = document["profiles"][:2]
    for profile in document["profiles"]:
        profile["initial_needs"] = {"toilet_urgency": 0.0, "hunger": 0.0, "fatigue": 0.0}
    document["scheduled_events"] = []
    document["model_configs"]["psychology"] = {
        "natural_recovery_rate_per_minute": 0.01,
        "natural_recovery_rate_by_agent": {"A001": 0.005, "A002": 0.05},
    }
    document["model_configs"]["policy_registry"] = {
        "schema_version": 1,
        "psychology": "mean-reverting-distress-v1",
    }
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    for agent in world.agents.values():
        agent.stress = 0.8
    result = create_psychology(world.model_configs).explain_update(WorldView.from_world(world), 0)
    by_id = {delta.agent_id: delta.stress for delta in result.deltas}
    assert by_id["A002"] < by_id["A001"]
    assert by_id["A001"] == pytest.approx(0.8 * math.exp(-0.005))
    assert by_id["A002"] == pytest.approx(0.8 * math.exp(-0.05))


def test_daily_latent_distress_accumulates_final_acute_values_and_is_checkpointable(
    research_psychology_scenario, stress_delta
) -> None:
    scenario = research_psychology_scenario(duration=1440)
    world = WorldState.from_scenario(scenario)
    state = world.model_state[PSYCHOLOGICAL_STATE_KEY]
    policy = create_psychology(world.model_configs)
    assert isinstance(policy, MeanRevertingDistressPolicy)
    for minute in range(1440):
        state = policy.update_long_term_state(
            state,
            minute,
            (stress_delta("A001", minute, 0.8), stress_delta("A002", minute, 0.2)),
        )
    world.model_state[PSYCHOLOGICAL_STATE_KEY] = state
    assert state["agents"]["A001"]["completed_days"] == 1
    assert state["agents"]["A001"]["long_term_distress"] == pytest.approx(0.084)
    world.minute_index = 1440
    world.validate()
    assert WorldState.from_dict(world.to_dict()).model_state == world.model_state


def test_integrated_life_state_policy_uses_one_exact_recovery_path_and_trace() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["id"] = "integrated-life-state-research-test"
    document["duration_minutes"] = 5
    document["profiles"] = document["profiles"][:2]
    document["scheduled_events"] = []
    document["model_configs"]["psychology"] = {
        "natural_recovery_rate_per_minute": 0.02,
        "sleeping_stress_recovery": 0.2,
        "baseline_distress_by_agent": {},
        "chronic_retention": 0.9,
        "chronic_acute_weight": 0.08,
        "chronic_high_distress_weight": 0.02,
        "high_distress_threshold": 0.7,
    }
    document["model_configs"]["policy_registry"] = {
        "schema_version": 1,
        "psychology": "mean-reverting-life-state-v1",
    }
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    agent = world.agents["A001"]
    agent.stress = 0.8
    agent.activity = Activity.SLEEPING
    agent.support_request_id = "support-wait-001"
    world.support_requests[agent.support_request_id] = SupportRequestState(
        id=agent.support_request_id,
        agent_id=agent.id,
        requested_minute=0,
        timeout_minute=20,
    )
    world.model_state["body_state_v1"]["agents"][agent.id]["sleep_debt_minutes"] = 300.0

    policy = create_psychology(world.model_configs)
    assert isinstance(policy, IntegratedDistressLifeStatePolicy)
    result = policy.explain_update(WorldView.from_world(world), 0)
    delta = next(item for item in result.deltas if item.agent_id == agent.id)
    trace = next(item for item in result.traces if item.agent_id == agent.id)
    components = dict(trace.component_contributions)
    assert trace.recovery_stress == 0.0
    assert components["natural_recovery"] < 0.0
    support_multiplier = (1.0 - math.exp(-0.02)) / 0.02
    assert components["staff_support_wait"] == pytest.approx(
        trace.support_wait_stress * support_multiplier
    )
    assert trace.support_wait_stress > 0.0
    assert trace.raw_stress_delta == pytest.approx(sum(components.values()))
    assert trace.stress_candidate_before_clip == pytest.approx(
        trace.stress_before + trace.raw_stress_delta
    )
    assert trace.psychology_clipped_delta == pytest.approx(delta.stress - agent.stress)
    assert delta.sleep_debt_minutes < 300.0
    forcing = (
        trace.need_pressure_stress
        + trace.queue_wait_stress
        + trace.support_wait_stress
        + trace.environmental_stress
        + trace.life_state_feedback_stress
        + trace.information_uncertainty_stress
    )
    assert delta.stress == pytest.approx(mean_reverting_step(agent.stress, 0.0, forcing, 0.02, 1.0))


def test_false_claim_pressure_reaches_uncertainty_stress_and_trace() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["id"] = "integrated-rumor-psychology-test"
    document["duration_minutes"] = 5
    document["profiles"] = document["profiles"][:2]
    document["scheduled_events"] = []
    document["model_configs"]["psychology"] = {
        "natural_recovery_rate_per_minute": 0.01,
        "baseline_distress_by_agent": {},
        "chronic_retention": 0.9,
        "chronic_acute_weight": 0.08,
        "chronic_high_distress_weight": 0.02,
        "high_distress_threshold": 0.7,
    }
    document["model_configs"]["information_spread"] = InformationSpreadConfig(
        enabled=True
    ).to_dict()
    document["model_configs"]["policy_registry"] = {
        "schema_version": 1,
        "psychology": "mean-reverting-life-state-v1",
        "information": "evidence-aware-rumor-v1",
    }
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    runtime = world.model_state[INFORMATION_SPREAD_STATE_KEY]
    message = TypedMessage(
        id="false-water-claim",
        truth_status=InformationTruthStatus.FALSE,
        source_id="social-source",
        zone_id=world.agents["A001"].zone_id,
        time=0,
        origin_event_id="rumor-event",
        origin_agent_id="A001",
    )
    runtime["messages"][message.id] = message.to_dict()
    runtime["beliefs"]["A001"][message.id] = AgentInformationState(
        message_id=message.id,
        belief=InformationBeliefStatus.HEARD,
        source="A001",
        last_contact=0,
        last_event_id="rumor-event",
        belief_strength=1.0,
    ).to_dict()
    world.model_state["body_state_v1"]["agents"]["A001"]["uncertainty"] = 0.2
    policy = create_psychology(world.model_configs)
    with_rumor = policy.explain_update(WorldView.from_world(world), 0)
    rumor_agent = next(item for item in with_rumor.deltas if item.agent_id == "A001")
    rumor_trace = next(item for item in with_rumor.traces if item.agent_id == "A001")

    runtime["beliefs"]["A001"][message.id] = AgentInformationState(
        message_id=message.id,
        belief=InformationBeliefStatus.REJECTED,
        source="trusted-staff",
        last_contact=1,
        last_event_id="correction-event",
        belief_strength=0.0,
        correction_exposed=True,
        correction_accepted=True,
        ever_misbelieved=True,
    ).to_dict()
    corrected = policy.explain_update(WorldView.from_world(world), 0)
    corrected_agent = next(item for item in corrected.deltas if item.agent_id == "A001")
    corrected_trace = next(item for item in corrected.traces if item.agent_id == "A001")

    assert rumor_trace.information_belief_pressure == 1.0
    assert rumor_trace.information_uncertainty_stress > 0.0
    assert rumor_trace.information_effect_sources == (
        (message.id, "rumor-event", "rumor-event", message.source_id, 0),
    )
    assert rumor_agent.uncertainty > corrected_agent.uncertainty
    assert rumor_agent.stress > corrected_agent.stress
    assert corrected_trace.information_belief_pressure == 0.0
    assert "information_belief_effect" in rumor_trace.to_dict(source_id=policy.source_id)


def test_psychology_trace_delta_fields_reconstruct_applied_stress_delta(
    research_psychology_scenario
) -> None:
    world = WorldState.from_scenario(research_psychology_scenario())
    agent = world.agents["A001"]
    agent.stress = 0.8
    policy = create_psychology(world.model_configs)
    result = policy.explain_update(WorldView.from_world(world), 0)
    trace = next(item for item in result.traces if item.agent_id == agent.id)
    serialized = trace.to_dict(source_id=policy.source_id)["stress"]
    assert sum(serialized["components"].values()) == pytest.approx(serialized["raw_delta"])
    assert (
        serialized["raw_delta"] + serialized["clip_delta"] + serialized["social_delta"]
    ) == pytest.approx(serialized["applied_delta"])
