"""Immutable policy views and reproducible behavior proposals.

Checkpoint replay of a behavior policy variant is checked in
tests/integration/test_behavior_policy.py.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from support.paths import BASELINE_100_7D, SCENARIO_100_AGENTS

from sheltersim import native
from sheltersim.core.rng import CounterRNG, KeyedRandom
from sheltersim.core.world import WorldState
from sheltersim.domain.models import Activity
from sheltersim.domain.staff import StaffService
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.behavior import (
    ActionCandidate,
    ActionKind,
    ActionProposal,
    AgentView,
    BehaviorDecisionRunner,
    IneligibleDecisionError,
    PriorityRuleBehaviorPolicy,
    UtilityBehaviorPolicy,
    WorldView,
    behavior_policy_catalog,
    create_behavior_policy,
)


def make_world() -> WorldState:
    return WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))


def test_world_view_gives_every_person_each_field_of_the_view(generated_scenario) -> None:
    """from_world fills a view's fields directly; none may fall back to the class default."""
    view = WorldView.from_world(WorldState.from_scenario(generated_scenario))
    names = set(AgentView.__dataclass_fields__)
    for agent in view.agents.values():
        assert set(agent.__dict__) == names
        assert AgentView(**agent.__dict__) == agent
        with pytest.raises(FrozenInstanceError):
            agent.stress = 1.0


def test_world_view_hash_is_the_same_with_and_without_the_kernel(
    generated_scenario, monkeypatch
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    with_kernel = WorldView.from_world(world).state_hash
    monkeypatch.setattr(native, "canonical_sha256", lambda value: None)
    assert WorldView.from_world(world).state_hash == with_kernel


def test_world_view_person_cache_follows_a_changed_record(generated_scenario) -> None:
    world = WorldState.from_scenario(generated_scenario)
    cache: dict[str, Any] = {}
    plain = WorldView.from_world(world)
    assert WorldView.from_world(world, person_cache=cache) == plain
    assert WorldView.from_world(world, person_cache=cache) == plain
    assert cache
    people = world.model_configs["population_context"]["people"]
    identity = sorted(people)[0]
    # An edit inside the record, not a new record, must also be seen.
    people[identity]["usual_wake_minute"] = 123
    view = WorldView.from_world(world, person_cache=cache)
    assert view.agents[identity].usual_wake_minute == 123
    assert view == WorldView.from_world(world)


def test_world_view_is_detached_read_only_and_canonical() -> None:
    world = make_world()
    view = WorldView.from_world(world)
    original_hash = view.state_hash

    with pytest.raises(TypeError):
        view.agents["other"] = view.agents["A001"]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        view.agents["A001"].stress = 0.7  # type: ignore[misc]

    world.agents["A001"].stress = 0.7
    assert view.agents["A001"].stress == 0.0
    assert view.state_hash == original_hash
    assert WorldView.from_world(make_world()).state_hash == original_hash


def test_behavior_proposal_is_reproducible_and_exposes_candidate_trace() -> None:
    world = make_world()
    agent = world.agents["A001"]
    agent.activity = Activity.IDLE
    agent.needs.toilet_urgency = 0.95
    view = WorldView.from_world(world)
    policy = UtilityBehaviorPolicy()

    first = policy.propose(view, "A001", CounterRNG(world.seed))
    second = policy.propose(view, "A001", CounterRNG(world.seed))
    candidates = {candidate.action: candidate for candidate in first.candidates}

    assert first == second
    assert first.observed_state_hash == view.state_hash
    assert first.rng_key == (
        "behavior",
        agent.profile.rng_key,
        "A001",
        "minute",
        0,
        "policy",
        policy.policy_version,
        "choice",
    )
    assert first.trigger == "scheduled_decision"
    assert candidates[ActionKind.REQUEST_TOILET].eligible
    assert candidates[ActionKind.REQUEST_TOILET].urgency_priority == 1
    assert (
        candidates[ActionKind.REQUEST_TOILET].trigger == "toilet_urgency>=0.25 or hygiene_need>=0.5"
    )
    assert sum(candidate.probability for candidate in first.candidates) == pytest.approx(1.0)
    assert first.selection_probability == candidates[first.action].probability


def test_behavior_policy_rejects_not_due_or_mutually_exclusive_active_agents() -> None:
    world = make_world()
    policy = UtilityBehaviorPolicy()
    agent = world.agents["A001"]

    agent.next_decision_at = 1
    with pytest.raises(IneligibleDecisionError, match="not due"):
        policy.propose(WorldView.from_world(world), agent.id, CounterRNG(world.seed))

    agent.next_decision_at = 0
    agent.activity = Activity.QUEUED
    with pytest.raises(IneligibleDecisionError, match="while queued"):
        policy.propose(WorldView.from_world(world), agent.id, CounterRNG(world.seed))


def test_behavior_policy_can_be_swapped_without_changing_runner() -> None:
    world = make_world()
    world.agents["A001"].activity = Activity.IDLE
    view = WorldView.from_world(world)

    class FixedActionPolicy:
        def __init__(self, action: ActionKind) -> None:
            self.action = action
            self.policy_version = f"fixed-{action.value}-v1"

        def propose(
            self,
            snapshot: WorldView,
            agent_id: str,
            rng: KeyedRandom,
            *,
            trigger: str = "scheduled_decision",
        ) -> ActionProposal:
            key = ("fixed-policy", self.policy_version, agent_id, snapshot.minute_index)
            choice_value = rng.random(*key)
            candidate = ActionCandidate(
                action=self.action,
                eligible=True,
                trigger="policy selected configured action",
                utility=1.0,
                urgency_priority=0,
                probability=1.0,
            )
            return ActionProposal(
                agent_id=agent_id,
                minute_index=snapshot.minute_index,
                observed_state_hash=snapshot.state_hash,
                action=self.action,
                candidates=(candidate,),
                trigger=trigger,
                choice_value=choice_value,
                rng_key=key,
                selection_probability=1.0,
                policy_version=self.policy_version,
                next_decision_at=snapshot.minute_index + 1,
            )

    wait = BehaviorDecisionRunner(FixedActionPolicy(ActionKind.WAIT)).propose_due(
        view, CounterRNG(world.seed)
    )
    rest = BehaviorDecisionRunner(FixedActionPolicy(ActionKind.REST)).propose_due(
        view, CounterRNG(world.seed)
    )

    assert wait[0].action is ActionKind.WAIT
    assert rest[0].action is ActionKind.REST
    assert wait[0].agent_id == rest[0].agent_id == "A001"


def test_registered_priority_policy_reuses_candidates_and_is_keyed_deterministic() -> None:
    world = make_world()
    world.agents["A001"].activity = Activity.IDLE
    world.agents["A001"].needs.toilet_urgency = 0.95
    world.agents["A001"].needs.hunger = 0.85
    before = world.canonical_json()
    view = WorldView.from_world(world)
    policy = create_behavior_policy("priority-rule-v1", temperature=0.8)

    first = policy.propose(view, "A001", CounterRNG(world.seed))
    second = policy.propose(view, "A001", CounterRNG(world.seed))
    candidates = [item for item in first.candidates if item.eligible]
    best_rank = max((item.urgency_priority, item.utility) for item in candidates)
    selected = next(item for item in candidates if item.action is first.action)

    assert isinstance(policy, PriorityRuleBehaviorPolicy)
    assert first == second
    assert world.canonical_json() == before
    assert first.policy_version == "priority-rule-v1"
    assert (selected.urgency_priority, selected.utility) == best_rank
    assert sum(item.probability for item in first.candidates) == pytest.approx(1.0)
    assert {item["variant"] for item in behavior_policy_catalog()} == {
        "utility-softmax-v1",
        "sleep-aware-utility-softmax-v2",
        "priority-rule-v1",
    }


def test_sleep_aware_behavior_normalizes_debt_by_daily_sleep_need() -> None:
    world = make_world()
    agent = world.agents["A001"]
    agent.activity = Activity.IDLE
    agent.needs.fatigue = 0.8
    body = world.model_state.setdefault("body_state_v1", {"agents": {}})
    body.setdefault("agents", {})[agent.id] = {"sleep_debt_minutes": 0.0}
    policy = create_behavior_policy(
        "sleep-aware-utility-softmax-v2",
    )
    low_debt = policy.propose(WorldView.from_world(world), agent.id, CounterRNG(world.seed))
    body["agents"][agent.id]["sleep_debt_minutes"] = 480.0
    high_debt = policy.propose(WorldView.from_world(world), agent.id, CounterRNG(world.seed))

    low_sleep = next(item for item in low_debt.candidates if item.action is ActionKind.SLEEP)
    high_sleep = next(item for item in high_debt.candidates if item.action is ActionKind.SLEEP)
    assert high_sleep.utility - low_sleep.utility == pytest.approx(1.0)
    assert high_sleep.probability > low_sleep.probability


def test_behavior_runner_only_proposes_due_agents_in_stable_id_order() -> None:
    world = make_world()
    for agent in world.agents.values():
        agent.activity = Activity.MOVING
    world.agents["A002"].activity = Activity.IDLE
    world.agents["A010"].activity = Activity.MOVING
    world.agents["A001"].activity = Activity.IDLE
    world.agents["A003"].activity = Activity.IDLE
    world.agents["A003"].next_decision_at = 5

    proposals = BehaviorDecisionRunner().propose_due(
        WorldView.from_world(world), CounterRNG(world.seed)
    )

    assert [proposal.agent_id for proposal in proposals] == ["A001", "A002"]


def test_support_candidates_are_suppressed_for_open_request_and_cooldown(
    causal_stress_scenario,
) -> None:
    scenario = causal_stress_scenario(agent_count=1)
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


def test_behavior_refuses_a_view_without_its_required_audit_hash() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(BASELINE_100_7D))
    view = WorldView.from_world(world, include_state_hash=False)

    with pytest.raises(ValueError, match="require a WorldView with a state hash"):
        UtilityBehaviorPolicy().propose(view, next(iter(world.agents)), CounterRNG(world.seed))


def test_a_directly_made_candidate_equals_the_dataclass_and_keeps_its_errors() -> None:
    from sheltersim.policies.behavior import _candidate

    made = _candidate(ActionKind.REST, True, "trigger", 0.25, 1, 0.5)
    assert made == ActionCandidate(ActionKind.REST, True, "trigger", 0.25, 1, 0.5)
    assert made.__dict__ == ActionCandidate(ActionKind.REST, True, "trigger", 0.25, 1, 0.5).__dict__
    for arguments in (
        (ActionKind.REST, True, "", 0.25, 1, 0.5),
        (ActionKind.REST, True, "t", float("nan"), 1, 0.5),
        (ActionKind.REST, True, "t", float("inf"), 1, 0.5),
        (ActionKind.REST, True, "t", 0.25, -1, 0.5),
        (ActionKind.REST, True, "t", 0.25, 1, float("nan")),
        (ActionKind.REST, True, "t", 0.25, 1, 1.5),
        (ActionKind.REST, False, "t", 0.25, 1, 0.5),
    ):
        with pytest.raises(ValueError):
            _candidate(*arguments)
    assert _candidate("rest", True, "t", 0.25, 1, 0.5).action is ActionKind.REST


def test_a_recorded_outcome_equals_replace_and_keeps_the_timing_check() -> None:
    from dataclasses import replace

    candidate = ActionCandidate(ActionKind.REST, True, "t", 0.25, 0, 1.0)
    proposal = ActionProposal(
        agent_id="A001",
        minute_index=5,
        observed_state_hash="0" * 64,
        action=ActionKind.REST,
        candidates=(candidate,),
        trigger="scheduled_decision",
        choice_value=0.5,
        rng_key=("behavior",),
        selection_probability=1.0,
        policy_version="v",
        next_decision_at=6,
    )
    assert proposal.with_outcome("X", 9) == replace(proposal, target_id="X", next_decision_at=9)
    with pytest.raises(ValueError):
        proposal.with_outcome("X", 5)


def test_a_kept_zone_environment_follows_the_configs_and_modifiers(generated_scenario) -> None:
    from sheltersim.domain.environment import ZONE_ENVIRONMENT_STATE_KEY, zone_environment_view
    from sheltersim.policies.behavior import _zone_environment

    world = WorldState.from_scenario(generated_scenario)
    world.model_state.setdefault(ZONE_ENVIRONMENT_STATE_KEY, {"schema_version": 1, "modifiers": {}})
    zone_id = next(iter(world.zones))
    cache: dict = {}
    first = _zone_environment(world, zone_id, cache)
    assert first == zone_environment_view(world, zone_id)
    assert _zone_environment(world, zone_id, cache) is first
    world.model_state[ZONE_ENVIRONMENT_STATE_KEY]["modifiers"]["m-1"] = {
        "zone_id": zone_id,
        "op": "set_lighting",
        "parameters": {"level": 0.125},
        "start_minute": 0,
        "start_event_id": "event-1",
        "source_id": "ASM-TEST",
        "assumption_ids": ["ASM-TEST"],
    }
    changed = _zone_environment(world, zone_id, cache)
    assert changed == zone_environment_view(world, zone_id)
    if "zone_environment" in world.model_configs:
        assert changed.lighting_level == 0.125
    del world.model_state[ZONE_ENVIRONMENT_STATE_KEY]["modifiers"]["m-1"]
    assert _zone_environment(world, zone_id, cache) == first
    world.model_configs = dict(world.model_configs)
    assert _zone_environment(world, zone_id, cache) == first
    assert cache["configs"] is world.model_configs
    del world.model_state[ZONE_ENVIRONMENT_STATE_KEY]
    assert _zone_environment(world, zone_id, cache) == zone_environment_view(world, zone_id)
    assert cache["has_state"] is False
