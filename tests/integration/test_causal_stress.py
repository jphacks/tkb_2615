"""Causal stress contributions, support gating, and trace reproducibility (H04)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from sheltersim.core.engine import TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.facilities import ToiletServiceManager
from sheltersim.domain.models import Activity
from sheltersim.domain.staff import StaffService
from sheltersim.events.handlers import EventDispatcher, FacilityOutageHandler
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import PsychologyModelConfig, ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.policies.behavior import ActionKind, UtilityBehaviorPolicy, WorldView
from sheltersim.policies.psychology import (
    PsychologyParameters,
    SimplePsychologyPolicy,
    apply_state_deltas,
)


def make_scenario(
    *,
    agent_count: int = 2,
    duration: int = 20,
    stress_pressure: float = 0.1,
    queued_stress: float = 0.01,
    support_wait_stress: float = 0.02,
    social_enabled: bool = False,
    with_service: bool = True,
) -> ScenarioSpec:
    profiles = [
        {
            "schema_version": 2,
            "id": f"A{index:03}",
            "group_id": "G01",
            "initial_zone_id": "main",
            "support_need": 0.8,
            "stress_sensitivity": 1.0,
            "initial_needs": {"toilet_urgency": 0.8, "hunger": 0.9, "fatigue": 0.2},
            "rng_key": f"profile:A{index:03}",
        }
        for index in range(1, agent_count + 1)
    ]
    config = {
        "psychology": {
            "stress_pressure": stress_pressure,
            "queued_stress": queued_stress,
            "support_wait_stress": support_wait_stress,
        },
        "behavior": {"decision_interval_minutes": 5},
        "staff": {"support_task_minutes": 5, "unresolved_after_minutes": 10},
        "social_propagation": {
            "enabled": social_enabled,
            "max_contacts_per_agent": 1,
            "minimum_contact_minutes": 1,
            "stress_spread_per_minute": 0.01,
            "natural_recovery_per_minute": 0.0,
            "cooldown_minutes": 1,
            "max_pair_stress_increase": 0.05,
            "max_agent_stress_increase_per_minute": 0.05,
        },
    }
    document = {
        "schema_version": 3,
        "id": "causal-stress-test",
        "duration_minutes": duration,
        "seed": 23,
        "zones": [{"id": "main", "name": "Main", "capacity": 10}],
        "profiles": profiles,
        "model_configs": config,
    }
    if with_service:
        document["facilities"] = [
            {"id": "toilet-1", "kind": "toilet", "zone_id": "main", "capacity": 1}
        ]
        document["staff"] = [{"id": "staff-1", "zone_id": "main", "skills": ["support"]}]
    return ScenarioSpec.model_validate(document)


def test_psychology_trace_separates_need_queue_and_staff_causes() -> None:
    scenario = make_scenario()
    world = WorldState.from_scenario(scenario)
    outage = ScheduledEvent(
        event_id="toilet-outage",
        kind=EventKind.EXOGENOUS,
        due_minute=0,
        priority=0,
        sequence=world.allocate_event_sequence(),
        event_type="facility_outage",
        payload={"facility_id": "toilet-1", "units_out": 1},
        source_id="test-scenario",
    )
    world.schedule_event(outage)
    EventDispatcher({"facility_outage": FacilityOutageHandler()}).dispatch_due(world, 0)

    world.agents["A001"].activity = Activity.IDLE
    ToiletServiceManager(world).request("A001", "toilet-1")
    support = StaffService(world, task_duration_minutes=5).request("A002")
    StaffService(world, task_duration_minutes=5).assign_pending(0)
    world.minute_index = 5

    view = WorldView.from_world(world)
    result = SimplePsychologyPolicy(
        PsychologyParameters(
            stress_pressure=0.1,
            queued_stress=0.01,
            support_wait_stress=0.02,
        )
    ).explain_update(view, 5)
    trace = result.trace_document(
        5,
        source_id="ASM-H04-CAUSAL-STRESS-V1",
        full=True,
    )
    by_agent = {item["agent_id"]: item for item in trace["agents"]}
    queue_trace = by_agent["A001"]
    support_trace = by_agent["A002"]

    assert queue_trace["stress"]["components"]["unmet_need_pressure"] > 0.0
    assert queue_trace["stress"]["components"]["facility_queue_wait"] == pytest.approx(0.01)
    queue_cause = next(
        item for item in queue_trace["direct_causes"] if item["kind"] == "facility_queue_wait"
    )
    assert queue_cause["wait_minutes"] == 5
    assert queue_cause["source_id"] == "ASM-TOILET-SERVICE-DURATION-V1"
    assert queue_cause["facility_capacity"] == 1
    assert queue_cause["facility_working_units"] == 0
    assert "toilet-outage" in queue_cause["capacity_event_context_ids"]
    assert support_trace["stress"]["components"]["staff_support_wait"] == pytest.approx(0.02)
    support_cause = next(
        item for item in support_trace["direct_causes"] if item["kind"] == "staff_support_wait"
    )
    assert support_cause["support_request_id"] == support.request_id
    assert support_cause["assigned_staff_id"] == "staff-1"
    assert support_trace["source_references"]["support_source_id"] == (
        "ASM-STAFF-SUPPORT-LIFECYCLE-V1"
    )

    capacity_before = world.facilities["toilet-1"].working_units
    apply_state_deltas(world, result.deltas)
    assert world.facilities["toilet-1"].working_units == capacity_before == 0


def test_scenario_psychology_coefficients_are_validated_and_source_tagged(tmp_path) -> None:
    scenario = make_scenario(stress_pressure=0.2)
    config = PsychologyModelConfig.model_validate(scenario.model_configs["psychology"])
    assert config.source_id == "ASM-H04-CAUSAL-STRESS-V1"
    assert config.policy_version == "causal-stress-v1"
    assert config.stress_pressure == pytest.approx(0.2)

    world = WorldState.from_scenario(scenario)
    writer = RunArtifactWriter(scenario, tmp_path / "run", trace_level=TraceLevel.OFF)
    writer.write_initial(world)
    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["model_assumptions"]["psychology"]["source_id"] == config.source_id
    assert manifest["model_assumptions"]["psychology"]["values"]["stress_pressure"] == 0.2

    view = WorldView.from_world(world)
    low_sensitivity = SimplePsychologyPolicy(
        PsychologyParameters(stress_pressure=0.01, support_wait_stress=0.0)
    ).update(view, 0)[0]
    high_sensitivity = SimplePsychologyPolicy(
        PsychologyParameters(stress_pressure=0.2, support_wait_stress=0.0)
    ).update(view, 0)[0]
    assert high_sensitivity.stress > low_sensitivity.stress

    with pytest.raises(ValidationError):
        make_scenario(stress_pressure=0.251)


def test_support_candidates_are_suppressed_for_open_request_and_cooldown() -> None:
    scenario = make_scenario(agent_count=1)
    world = WorldState.from_scenario(scenario)
    agent = world.agents["A001"]
    agent.activity = Activity.IDLE
    receipt = StaffService(world).request(agent.id)
    agent.activity = Activity.IDLE  # Keep the proposal gate under test while request is open.
    policy = UtilityBehaviorPolicy()

    proposal = policy.propose(WorldView.from_world(world), agent.id, CounterRNG(world.seed))
    help_candidate = next(
        item for item in proposal.candidates if item.action is ActionKind.SEEK_HELP
    )
    assert not help_candidate.eligible
    assert world.support_requests[receipt.request_id].status.value == "pending"

    agent.support_request_id = None
    agent.support_cooldown_until = 10
    cooldown_proposal = policy.propose(
        WorldView.from_world(world), agent.id, CounterRNG(world.seed)
    )
    assert not next(
        item for item in cooldown_proposal.candidates if item.action is ActionKind.SEEK_HELP
    ).eligible

    world.minute_index = 10
    eligible_proposal = policy.propose(
        WorldView.from_world(world), agent.id, CounterRNG(world.seed)
    )
    assert next(
        item for item in eligible_proposal.candidates if item.action is ActionKind.SEEK_HELP
    ).eligible


def test_trace_levels_and_social_policy_replay_deterministically(tmp_path) -> None:
    scenario = make_scenario(duration=12, with_service=False)
    worlds = {}
    for level in (TraceLevel.OFF, TraceLevel.SUMMARY, TraceLevel.FULL):
        output = tmp_path / level.value
        worlds[level] = run_scenario(scenario, output, trace_level=level)
        trace_path = output / "psychology_contributions.jsonl"
        if level is TraceLevel.OFF:
            assert not trace_path.exists()
        else:
            records = [json.loads(line) for line in trace_path.read_text().splitlines()]
            assert records
            assert all(record["social_propagation_is_separate"] for record in records)
    final_hashes = {world.canonical_json() for world in worlds.values()}
    assert len(final_hashes) == 1
    assert all(len(world.support_requests) <= len(world.agents) for world in worlds.values())

    social_scenario = make_scenario(duration=12, social_enabled=True, with_service=False)
    first = run_scenario(social_scenario, tmp_path / "social-1", trace_level=TraceLevel.FULL)
    second = run_scenario(social_scenario, tmp_path / "social-2", trace_level=TraceLevel.FULL)
    assert first.canonical_json() == second.canonical_json()
    contacts = (tmp_path / "social-1" / "social_contacts.jsonl").read_text(encoding="utf-8")
    assert contacts == (tmp_path / "social-2" / "social_contacts.jsonl").read_text(encoding="utf-8")
