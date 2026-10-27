"""Deterministic and bounded contact-based stress propagation tests.

A full Run with the policy enabled is checked in
tests/integration/test_stress_propagation.py.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.models import (
    Activity,
    Mobility,
)
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.behavior import AgentView, NeedView, WorldView
from sheltersim.policies.psychology import (
    AgentStateDelta,
    SimplePsychologyPolicy,
)
from sheltersim.policies.registry import create_social
from sheltersim.policies.social import (
    SocialPropagationParameters,
    StressPropagationPolicy,
)


def _two_agent_world() -> WorldState:
    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["duration_minutes"] = 20
    document["scheduled_events"] = []
    document["profiles"] = document["profiles"][:2]
    document["model_configs"] = {}
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    ids = sorted(world.agents)
    world.agents[ids[0]].stress = 0.1
    world.agents[ids[1]].stress = 0.9
    return world


def _base_deltas(view: WorldView, minute: int) -> tuple[AgentStateDelta, ...]:
    if view.minute_index != minute:
        view = replace(view, minute_index=minute)
    return SimplePsychologyPolicy().update(view, minute)


def test_disabled_model_preserves_the_rule_baseline_exactly() -> None:
    world = _two_agent_world()
    view = WorldView.from_world(world)
    baseline = _base_deltas(view, 0)

    result = StressPropagationPolicy(SocialPropagationParameters(enabled=False)).update(
        view,
        baseline,
        CounterRNG(world.seed),
        zone_neighbors={},
    )

    assert result.deltas == baseline
    assert result.contacts == ()
    assert result.runtime_state == {}


def test_family_contact_propagates_stress_and_records_contribution() -> None:
    world = _two_agent_world()
    view = WorldView.from_world(world)
    policy = StressPropagationPolicy(
        SocialPropagationParameters(
            enabled=True,
            max_contacts_per_agent=1,
            minimum_contact_minutes=1,
            stress_spread_per_minute=0.1,
            natural_recovery_per_minute=0,
            cooldown_minutes=5,
        )
    )
    baseline = _base_deltas(view, 0)

    result = policy.update(
        view,
        baseline,
        CounterRNG(world.seed),
        zone_neighbors={},
    )

    assert result.contributing_contact_count == 1
    assert result.total_stress_added > 0
    assert any(item.contact_basis == "family" for item in result.contacts)
    trace = result.trace_document(0)
    assert trace["selected_contact_count"] == len(result.contacts)
    assert trace["contacts"][0]["stress_contribution"] > 0
    assert len(trace["contributions"]) == result.contributing_contact_count
    assert result.runtime_state["schema_version"] == 1


def test_agent_cooldown_suppresses_repeated_influence_until_it_expires() -> None:
    world = _two_agent_world()
    policy = StressPropagationPolicy(
        SocialPropagationParameters(
            enabled=True,
            max_contacts_per_agent=1,
            minimum_contact_minutes=1,
            stress_spread_per_minute=0.1,
            natural_recovery_per_minute=0,
            cooldown_minutes=5,
        )
    )
    runtime_state = None
    first_result = None
    for minute in range(6):
        view = replace(WorldView.from_world(world), minute_index=minute)
        deltas = _base_deltas(view, minute)
        result = policy.update(
            view,
            deltas,
            CounterRNG(world.seed),
            zone_neighbors={},
            runtime_state=runtime_state,
        )
        runtime_state = result.runtime_state
        if minute == 0:
            first_result = result
        elif minute < 5:
            assert result.contributing_contact_count == 0
        elif minute == 5:
            assert result.contributing_contact_count == 1
    assert first_result is not None and first_result.total_stress_added > 0


def test_nearby_zone_contacts_are_selected_without_family_membership() -> None:
    world = _two_agent_world()
    original = WorldView.from_world(world)
    agents = {
        agent_id: replace(agent, group_id=None, zone_id="sleeping" if index == 0 else "corridor")
        for index, (agent_id, agent) in enumerate(sorted(original.agents.items()))
    }
    view = WorldView(0, agents, original.facilities, original.state_hash)
    base = _base_deltas(view, 0)
    policy = StressPropagationPolicy(
        SocialPropagationParameters(enabled=True, max_contacts_per_agent=1)
    )

    result = policy.update(
        view,
        base,
        CounterRNG(world.seed),
        zone_neighbors={"sleeping": ("corridor",), "corridor": ("sleeping",)},
    )

    assert result.contacts
    assert all(item.contact_basis == "nearby_zone" for item in result.contacts)


def test_contact_graph_is_seed_reproducible_and_bounded_for_1000_agents() -> None:
    agents = {
        f"A{index:04d}": AgentView(
            id=f"A{index:04d}",
            group_id="one-large-group",
            zone_id="one-large-zone",
            mobility=Mobility.INDEPENDENT,
            support_need=0.0,
            stress_sensitivity=1.0,
            rng_key=f"profile:A{index:04d}",
            activity=Activity.IDLE,
            target_id=None,
            needs=NeedView(0.0, 0.0, 0.0),
            stress=(index % 100) / 100,
            trust=0.5,
            next_decision_at=0,
        )
        for index in range(1000)
    }
    view = WorldView(0, agents, {}, "a" * 64)
    deltas = tuple(
        AgentStateDelta(
            agent_id=agent_id,
            minute_index=0,
            toilet_urgency=0.0,
            hunger=0.0,
            fatigue=0.0,
            stress=agent.stress,
            trust=agent.trust,
        )
        for agent_id, agent in agents.items()
    )
    policy = StressPropagationPolicy(
        SocialPropagationParameters(enabled=True, max_contacts_per_agent=3)
    )

    first = policy.update(view, deltas, CounterRNG(2026), zone_neighbors={})
    second = policy.update(view, deltas, CounterRNG(2026), zone_neighbors={})

    assert first == second
    assert len(first.contacts) <= len(agents) * 3
    assert len(first.runtime_state["contacts"]) <= len(agents) * 3
    assert len(first.runtime_state["agent_cooldown_until"]) <= len(agents)


def test_coefficients_are_bounded_and_provenance_is_explicit() -> None:
    policy = StressPropagationPolicy(
        SocialPropagationParameters(enabled=True, stress_spread_per_minute=0.01)
    )

    provenance = policy.parameters.provenance()

    assert provenance["source_type"] == "assumption"
    assert provenance["source_id"] == "ASM-SOCIAL-PROPAGATION-V1"
    assert provenance["coefficients"]["stress_spread_per_minute"] == 0.01


def test_bosse_operator_is_bidirectional_bounded_and_independent_of_mapping_order(
    research_psychology_scenario, stress_delta
) -> None:
    world = WorldState.from_scenario(research_psychology_scenario(social=True))
    ordered = sorted(world.agents)
    world.agents[ordered[0]].stress = 0.1
    world.agents[ordered[1]].stress = 0.9
    view = WorldView.from_world(world)
    reversed_view = replace(view, agents=dict(reversed(list(view.agents.items()))))
    policy = create_social(world.model_configs)
    assert policy.parameters.natural_recovery_per_minute == 0.0
    deltas = tuple(stress_delta(agent_id, 0, view.agents[agent_id].stress) for agent_id in ordered)

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


def test_bosse_operator_has_no_effect_without_a_local_contact_and_caps_each_tick(
    research_psychology_scenario, stress_delta
) -> None:
    world = WorldState.from_scenario(research_psychology_scenario(social=True))
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
    deltas = tuple(stress_delta(agent_id, 0, view.agents[agent_id].stress) for agent_id in ordered)
    no_contact = policy.update(view, deltas, CounterRNG(5), zone_neighbors={})
    assert no_contact.contacts == ()
    assert no_contact.deltas == deltas

    equal_stress_view = replace(
        original,
        agents={agent_id: replace(original.agents[agent_id], stress=0.6) for agent_id in ordered},
    )
    equal_stress_deltas = tuple(stress_delta(agent_id, 0, 0.6) for agent_id in ordered)
    equal_state = policy.update(
        equal_stress_view,
        equal_stress_deltas,
        CounterRNG(5),
        zone_neighbors={},
    )
    assert all(delta.stress == pytest.approx(0.6) for delta in equal_state.deltas)

    together = WorldState.from_scenario(research_psychology_scenario(social=True))
    ids = sorted(together.agents)
    together.agents[ids[0]].stress = 0.0
    together.agents[ids[1]].stress = 1.0
    contact_view = WorldView.from_world(together)
    contact_deltas = tuple(
        stress_delta(agent_id, 0, contact_view.agents[agent_id].stress) for agent_id in ids
    )
    bounded = policy.update(contact_view, contact_deltas, CounterRNG(6), zone_neighbors={})
    assert all(
        abs(row.stress - contact_view.agents[row.agent_id].stress) <= 0.05 for row in bounded.deltas
    )


def test_bosse_trace_matches_normalized_weighted_synchrony_formula(
    research_psychology_scenario, stress_delta
) -> None:
    world = WorldState.from_scenario(research_psychology_scenario(social=True))
    ids = sorted(world.agents)
    world.agents[ids[0]].stress = 0.15
    world.agents[ids[1]].stress = 0.85
    view = WorldView.from_world(world)
    policy = create_social(world.model_configs)
    deltas = tuple(stress_delta(agent_id, 0, view.agents[agent_id].stress) for agent_id in ids)
    result = policy.update(view, deltas, CounterRNG(19), zone_neighbors={})
    by_recipient: dict[str, list[object]] = {}
    for item in result.contacts:
        by_recipient.setdefault(item.recipient_id, []).append(item)
    for contacts in by_recipient.values():
        total_weight = math.fsum(item.influence_weight for item in contacts)
        if total_weight == 0.0:
            assert all(item.stress_contribution == 0.0 for item in contacts)
            continue
        gamma = min(1.0, policy.parameters.stress_spread_per_minute * total_weight)
        assert all(item.aggregate_rate == pytest.approx(gamma) for item in contacts)
        for item in contacts:
            raw = gamma * item.influence_weight / total_weight * item.stress_difference
            assert item.pre_pair_cap_contribution == pytest.approx(raw)
            assert item.pair_cap_contribution == pytest.approx(
                max(
                    -policy.parameters.max_pair_stress_increase,
                    min(policy.parameters.max_pair_stress_increase, raw),
                )
            )
