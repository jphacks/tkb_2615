"""Deterministic and bounded contact-based stress propagation tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.models import Activity, Mobility
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.behavior import AgentView, NeedView, WorldView
from sheltersim.policies.psychology import (
    AgentStateDelta,
    SimplePsychologyPolicy,
)
from sheltersim.policies.social import (
    SocialPropagationParameters,
    StressPropagationPolicy,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


def _two_agent_world() -> WorldState:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
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


def test_enabled_policy_is_checkpointable_and_writes_compact_contact_trace(
    tmp_path: Path,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["duration_minutes"] = 3
    document["scheduled_events"] = []
    document["profiles"] = document["profiles"][:2]
    document["model_configs"]["social_propagation"] = {
        "enabled": True,
        "max_contacts_per_agent": 1,
        "minimum_contact_minutes": 1,
        "stress_spread_per_minute": 0.01,
        "natural_recovery_per_minute": 0.001,
        "cooldown_minutes": 5,
    }
    scenario = ScenarioSpec.model_validate(document)

    world = run_scenario(scenario, tmp_path / "social-run")
    untraced_world = run_scenario(
        scenario,
        tmp_path / "social-run-off-trace",
        trace_level=TraceLevel.OFF,
    )

    assert world.schema_version == 5
    assert world.canonical_json() == untraced_world.canonical_json()
    assert world.model_state["social_propagation_v1"]["schema_version"] == 1
    restored = Checkpoint.capture(world).restore_world()
    assert restored.model_state == world.model_state
    assert restored.canonical_json() == world.canonical_json()
    manifest = json.loads((tmp_path / "social-run" / "manifest.json").read_text())
    assert (
        manifest["model_assumptions"]["social_propagation"]["source_id"]
        == "ASM-SOCIAL-PROPAGATION-V1"
    )
    traces = [
        json.loads(line)
        for line in (tmp_path / "social-run" / "social_contacts.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(traces) == 3
    assert all(item["selected_contact_count"] <= 2 for item in traces)
    assert all(len(item["contacts"]) <= 20 for item in traces)
    assert (tmp_path / "social-run-off-trace" / "social_contacts.jsonl").read_text(
        encoding="utf-8"
    ) == ""
