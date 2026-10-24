"""Internal checks for the opt-in research-inspired distress and social operators."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.distress_monitor import (
    distress_monitor_summary,
    record_distress_monitor_tick,
)
from sheltersim.domain.information import (
    INFORMATION_SPREAD_STATE_KEY,
    create_information_runtime_state,
    information_spread_summary,
    upgrade_information_runtime_state,
    validate_information_runtime_state,
)
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
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.policies.behavior import WorldView
from sheltersim.policies.psychology import (
    AgentStateDelta,
    IntegratedDistressLifeStatePolicy,
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

    equal_stress_view = replace(
        original,
        agents={agent_id: replace(original.agents[agent_id], stress=0.6) for agent_id in ordered},
    )
    equal_stress_deltas = tuple(_delta(agent_id, 0, 0.6) for agent_id in ordered)
    equal_state = policy.update(
        equal_stress_view,
        equal_stress_deltas,
        CounterRNG(5),
        zone_neighbors={},
    )
    assert all(delta.stress == pytest.approx(0.6) for delta in equal_state.deltas)

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


def test_bosse_trace_matches_normalized_weighted_synchrony_formula() -> None:
    world = WorldState.from_scenario(_scenario(social=True))
    ids = sorted(world.agents)
    world.agents[ids[0]].stress = 0.15
    world.agents[ids[1]].stress = 0.85
    view = WorldView.from_world(world)
    policy = create_social(world.model_configs)
    deltas = tuple(_delta(agent_id, 0, view.agents[agent_id].stress) for agent_id in ids)
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


def test_full_runner_recovers_initially_high_agents_and_records_online_recovery(
    tmp_path: Path,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["id"] = "integrated-high-distress-recovery"
    document["duration_minutes"] = 15
    document["profiles"] = document["profiles"][:2]
    document["scheduled_events"] = []
    document["model_configs"]["psychology"] = {
        "natural_recovery_rate_per_minute": 0.02,
        "baseline_distress_by_agent": {},
        "chronic_retention": 0.9,
        "chronic_acute_weight": 0.08,
        "chronic_high_distress_weight": 0.02,
        "high_distress_threshold": 0.7,
    }
    document["model_configs"]["policy_registry"] = {
        "schema_version": 1,
        "psychology": "mean-reverting-life-state-v1",
        "social": "expressiveness-channel-openness-v1",
    }
    document["model_configs"]["social_propagation"] = {
        "enabled": False,
        "max_contacts_per_agent": 0,
        "minimum_contact_minutes": 1,
        "stress_spread_per_minute": 0.0,
        "natural_recovery_per_minute": 0.0,
    }
    scenario = ScenarioSpec.model_validate(document)
    world = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(world)
    for agent in world.agents.values():
        agent.stress = 0.8
    artifacts = RunArtifactWriter(
        scenario, tmp_path / "high-distress-recovery", trace_level=TraceLevel.OFF
    )
    artifacts.write_initial(world, execution_mode="offline")
    controller = OfflineSimulationController(world, artifacts)

    TickEngine(world, phase_handler=controller).run()
    artifacts.finalize(world)

    assert world.minute_index == scenario.duration_minutes
    assert all(0.0 <= agent.stress < 0.8 for agent in world.agents.values())
    assert all(math.isfinite(agent.stress) for agent in world.agents.values())
    monitor = distress_monitor_summary(world.metric_state)
    assert monitor["completed_recovery_count"] == len(world.agents)
    assert monitor["mean_completed_recovery_minutes"] > 0


def test_full_runner_combines_social_synchrony_with_natural_recovery(
    tmp_path: Path,
) -> None:
    scenario = _scenario(duration=4, social=True)
    document = scenario.model_dump(mode="json")
    document["model_configs"]["policy_registry"]["psychology"] = "mean-reverting-life-state-v1"
    scenario = ScenarioSpec.model_validate(document)
    world = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(world)
    high_id, low_id = sorted(world.agents)
    world.agents[high_id].stress = 0.8
    world.agents[low_id].stress = 0.1
    run_dir = tmp_path / "social-natural-recovery"
    artifacts = RunArtifactWriter(scenario, run_dir, trace_level=TraceLevel.FULL)
    artifacts.write_initial(world, execution_mode="offline")
    controller = OfflineSimulationController(world, artifacts)

    TickEngine(world, phase_handler=controller).run()
    artifacts.finalize(world)

    traces = [
        json.loads(line)
        for line in (run_dir / "psychology_contributions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    first_tick = next(row for row in traces if row["minute_index"] == 0)
    first_agents = {row["agent_id"]: row for row in first_tick["agents"]}
    assert first_agents[high_id]["stress"]["social_delta"] < 0.0
    assert first_agents[low_id]["stress"]["social_delta"] > 0.0
    for row in first_agents.values():
        stress = row["stress"]
        assert stress["applied_delta"] == pytest.approx(
            stress["psychology_clipped_delta"] + stress["social_delta"]
        )
    assert world.agents[high_id].stress < 0.8
    assert world.agents[low_id].stress < 0.8
    assert world.agents[low_id].stress > 0.1


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


def test_information_summary_separates_current_wrong_belief_from_forgotten_history() -> None:
    state = create_information_runtime_state({"A001"})
    message = TypedMessage(
        id="false-claim",
        truth_status=InformationTruthStatus.FALSE,
        source_id="source",
        zone_id="zone",
        time=0,
        origin_event_id="rumor",
        origin_agent_id="A001",
    )
    state["messages"][message.id] = message.to_dict()
    state["beliefs"]["A001"][message.id] = AgentInformationState(
        message_id=message.id,
        belief=InformationBeliefStatus.UNKNOWN,
        source="A001",
        last_contact=0,
        last_event_id="rumor",
        belief_strength=1.0,
        share_propensity=0.0,
        ever_misbelieved=True,
        ever_forgotten=True,
    ).to_dict()
    summary = information_spread_summary(state, agent_count=1)
    assert summary["wrong_belief_count"] == 0
    assert summary["current_wrong_belief_count"] == 0
    assert summary["ever_wrong_belief_count"] == 1
    assert summary["currently_forgotten_count"] == 1
    assert summary["ever_forgotten_count"] == 1


def test_information_runtime_v1_checkpoint_migrates_history_fields_and_validates() -> None:
    state = create_information_runtime_state({"A001"})
    message = TypedMessage(
        id="false-claim",
        truth_status=InformationTruthStatus.FALSE,
        source_id="source",
        zone_id="zone",
        time=0,
        origin_event_id="rumor",
        origin_agent_id="A001",
    )
    state["messages"][message.id] = message.to_dict()
    record = AgentInformationState(
        message_id=message.id,
        belief=InformationBeliefStatus.UNKNOWN,
        source="A001",
        last_contact=0,
        last_event_id="rumor",
        belief_strength=1.0,
        share_propensity=0.0,
        ever_misbelieved=True,
        ever_forgotten=True,
    ).to_dict()
    record.pop("ever_misbelieved")
    record.pop("ever_forgotten")
    record.pop("last_correction_accepted")
    state["beliefs"]["A001"][message.id] = record
    state["schema_version"] = 1

    upgrade_information_runtime_state(state)
    validate_information_runtime_state(
        state, agent_ids={"A001"}, zone_ids={"zone"}, duration_minutes=10
    )
    migrated = AgentInformationState.from_dict(state["beliefs"]["A001"][message.id])
    assert state["schema_version"] == 2
    assert migrated.ever_misbelieved is True
    assert migrated.ever_forgotten is True


def test_online_distress_monitor_tracks_high_runs_recovery_and_reascent() -> None:
    metrics: dict[str, float] = {}
    record_distress_monitor_tick(
        metrics,
        previous_by_agent={"A001": 0.8},
        current_by_agent={"A001": 0.85},
        threshold=0.7,
    )
    record_distress_monitor_tick(
        metrics,
        previous_by_agent={"A001": 0.85},
        current_by_agent={"A001": 0.9},
        threshold=0.7,
    )
    record_distress_monitor_tick(
        metrics,
        previous_by_agent={"A001": 0.9},
        current_by_agent={"A001": 0.3},
        threshold=0.7,
    )
    record_distress_monitor_tick(
        metrics,
        previous_by_agent={"A001": 0.3},
        current_by_agent={"A001": 0.8},
        threshold=0.7,
    )
    summary = distress_monitor_summary(metrics)
    assert summary["high_distress_episode_count"] == 2
    assert summary["high_distress_reascent_count"] == 1
    assert summary["longest_high_distress_run_minutes"] == 2
    assert summary["completed_recovery_count"] == 1
    assert summary["mean_completed_recovery_minutes"] == 2.0


def test_psychology_trace_delta_fields_reconstruct_applied_stress_delta() -> None:
    world = WorldState.from_scenario(_scenario())
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
