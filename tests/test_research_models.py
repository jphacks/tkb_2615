"""Internal checks for the opt-in research-inspired distress and social operators."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.psychological_state import PSYCHOLOGICAL_STATE_KEY
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.behavior import WorldView
from sheltersim.policies.psychology import (
    AgentStateDelta,
    MeanRevertingDistressPolicy,
    mean_reverting_step,
)
from sheltersim.policies.registry import create_psychology, create_social

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures" / "scenario_100_agents.json"


def _scenario(*, duration: int = 20, social: bool = False) -> ScenarioSpec:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["id"] = "research-model-test"
    document["duration_minutes"] = duration
    document["scheduled_events"] = []
    document["profiles"] = document["profiles"][:2]
    for profile in document["profiles"]:
        profile["initial_needs"] = {
            "toilet_urgency": 0.0,
            "hunger": 0.0,
            "fatigue": 0.0,
        }
    document["model_configs"]["psychology"] = {
        "natural_recovery_rate_per_minute": 0.02,
        "chronic_retention": 0.9,
        "chronic_acute_weight": 0.08,
        "chronic_high_distress_weight": 0.02,
        "high_distress_threshold": 0.7,
    }
    document["model_configs"]["policy_registry"] = {
        "schema_version": 1,
        "psychology": "mean-reverting-distress-v1",
    }
    if social:
        document["model_configs"]["social_propagation"] = {
            "enabled": True,
            "max_contacts_per_agent": 1,
            "minimum_contact_minutes": 1,
            "stress_spread_per_minute": 0.2,
            "natural_recovery_per_minute": 0.0,
            "cooldown_minutes": 5,
            "max_pair_stress_increase": 0.25,
            "max_agent_stress_increase_per_minute": 0.05,
            "source_id": "ASM-BOSSE-AFFECT-CONTAGION-V1",
        }
        document["model_configs"]["policy_registry"]["social"] = (
            "expressiveness-channel-openness-v1"
        )
    return ScenarioSpec.model_validate(document)


def _delta(agent_id: str, minute: int, stress: float) -> AgentStateDelta:
    return AgentStateDelta(
        agent_id=agent_id,
        minute_index=minute,
        toilet_urgency=0.0,
        hunger=0.0,
        fatigue=0.0,
        stress=stress,
        trust=0.5,
    )


def test_acute_distress_is_a_compatibility_alias_and_recovers_at_restless_ticks() -> None:
    world = WorldState.from_scenario(_scenario())
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
        same_shock = [
            mean_reverting_step(value, 0.0, 0.0, 0.01, 1.0) for value in same_shock
        ]
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


def test_daily_latent_distress_accumulates_final_acute_values_and_is_checkpointable() -> None:
    scenario = _scenario(duration=1440)
    world = WorldState.from_scenario(scenario)
    state = world.model_state[PSYCHOLOGICAL_STATE_KEY]
    policy = create_psychology(world.model_configs)
    assert isinstance(policy, MeanRevertingDistressPolicy)
    for minute in range(1440):
        state = policy.update_long_term_state(
            state,
            minute,
            (_delta("A001", minute, 0.8), _delta("A002", minute, 0.2)),
        )
    world.model_state[PSYCHOLOGICAL_STATE_KEY] = state
    assert state["agents"]["A001"]["completed_days"] == 1
    assert state["agents"]["A001"]["long_term_distress"] == pytest.approx(0.084)
    world.minute_index = 1440
    world.validate()
    assert WorldState.from_dict(world.to_dict()).model_state == world.model_state


def test_bosse_operator_is_bidirectional_bounded_and_independent_of_mapping_order() -> None:
    world = WorldState.from_scenario(_scenario(social=True))
    ordered = sorted(world.agents)
    world.agents[ordered[0]].stress = 0.1
    world.agents[ordered[1]].stress = 0.9
    view = WorldView.from_world(world)
    reversed_view = replace(view, agents=dict(reversed(list(view.agents.items()))))
    policy = create_social(world.model_configs)
    assert policy.parameters.natural_recovery_per_minute == 0.0
    deltas = tuple(_delta(agent_id, 0, view.agents[agent_id].stress) for agent_id in ordered)

    first = policy.update(view, deltas, CounterRNG(world.seed), zone_neighbors={})
    reversed_result = policy.update(
        reversed_view, deltas, CounterRNG(world.seed), zone_neighbors={}
    )
    assert first == reversed_result
    after = {delta.agent_id: delta.stress for delta in first.deltas}
    assert after[ordered[0]] > view.agents[ordered[0]].stress
    assert after[ordered[1]] < view.agents[ordered[1]].stress
    assert all(0.0 <= value <= 1.0 for value in after.values())
    assert all(abs(item.stress_contribution) <= 0.25 for item in first.contacts)


def test_bosse_operator_has_no_effect_without_a_local_contact_and_caps_each_tick() -> None:
    world = WorldState.from_scenario(_scenario(social=True))
    original = WorldView.from_world(world)
    ordered = sorted(original.agents)
    isolated_agents = {
        agent_id: replace(
            original.agents[agent_id],
            group_id=None,
            zone_id=f"isolated-{index}",
            stress=0.2 + 0.6 * index,
        )
        for index, agent_id in enumerate(ordered)
    }
    view = replace(original, agents=isolated_agents)
    policy = create_social(world.model_configs)
    deltas = tuple(_delta(agent_id, 0, view.agents[agent_id].stress) for agent_id in ordered)
    no_contact = policy.update(view, deltas, CounterRNG(5), zone_neighbors={})
    assert no_contact.contacts == ()
    assert no_contact.deltas == deltas

    together = WorldState.from_scenario(_scenario(social=True))
    ids = sorted(together.agents)
    together.agents[ids[0]].stress = 0.0
    together.agents[ids[1]].stress = 1.0
    contact_view = WorldView.from_world(together)
    contact_deltas = tuple(
        _delta(agent_id, 0, contact_view.agents[agent_id].stress) for agent_id in ids
    )
    bounded = policy.update(contact_view, contact_deltas, CounterRNG(6), zone_neighbors={})
    assert all(
        abs(row.stress - contact_view.agents[row.agent_id].stress) <= 0.05 for row in bounded.deltas
    )
