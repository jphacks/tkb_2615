"""Agent state, immutable policy views, and reproducible behavior tests."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.rng import CounterRNG, KeyedRandom
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.models import Activity, AgentState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.policies.behavior import (
    ActionCandidate,
    ActionKind,
    ActionProposal,
    BehaviorDecisionRunner,
    IneligibleDecisionError,
    PriorityRuleBehaviorPolicy,
    UtilityBehaviorPolicy,
    WorldView,
    behavior_policy_catalog,
    create_behavior_policy,
)
from sheltersim.policies.psychology import (
    AgentStateDelta,
    SimplePsychologyPolicy,
    apply_state_deltas,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


def make_world() -> WorldState:
    return WorldState.from_scenario(ScenarioSpec.load(FIXTURE))


def test_world_state_v5_serializes_bounded_psychology_and_decision_time() -> None:
    world = make_world()
    agent = world.agents["A001"]
    agent.stress = 0.35
    agent.trust = 0.72
    agent.next_decision_at = 23

    serialized = world.canonical_json()

    assert world.schema_version == 5
    assert '"schema_version":5' in serialized
    assert '"stress":0.35' in serialized
    assert '"trust":0.72' in serialized
    assert '"next_decision_at":23' in serialized


@pytest.mark.parametrize(
    ("field", "value"),
    [("stress", -0.1), ("stress", 1.1), ("trust", float("nan"))],
)
def test_world_rejects_psychology_values_outside_zero_to_one(field: str, value: float) -> None:
    world = make_world()
    setattr(world.agents["A001"], field, value)

    with pytest.raises(StateInvariantError, match=f"invalid {field}"):
        world.validate()


def test_agent_rejects_invalid_next_decision_time() -> None:
    agent = make_world().agents["A001"]

    with pytest.raises(ValueError, match="next_decision_at"):
        AgentState(
            profile=agent.profile,
            zone_id=agent.zone_id,
            next_decision_at=-1,
        )


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
        "priority-rule-v1",
    }


def test_priority_policy_checkpoint_noop_fork_replays_with_same_variant(tmp_path: Path) -> None:
    scenario = ScenarioSpec.load(FIXTURE).with_behavior_policy_variant("priority-rule-v1")
    root = BranchRuntime.from_world(WorldState.from_scenario(scenario), branch_id="source")
    checkpoint = root.checkpoint()
    uninterrupted = BranchRuntime.from_checkpoint(checkpoint, branch_id="continued")
    forked = BranchRuntime.from_checkpoint(checkpoint, branch_id="forked")
    outputs = []

    for index, branch in enumerate((uninterrupted, forked)):
        writer = RunArtifactWriter(
            scenario,
            tmp_path / f"branch-{index}",
            trace_level=TraceLevel.FULL,
        )
        writer.write_initial(branch.world, execution_mode="offline")
        controller = OfflineSimulationController(branch.world, writer)
        TickEngine(branch.world, phase_handler=controller).run(steps=75)
        writer.finalize(branch.world)
        outputs.append((branch, writer))

    assert outputs[0][0].world.canonical_json() == outputs[1][0].world.canonical_json()
    assert outputs[0][0].world.event_log.records == outputs[1][0].world.event_log.records
    assert outputs[0][1].metrics.to_dict() == outputs[1][1].metrics.to_dict()
    trace = json.loads((tmp_path / "branch-0" / "decisions.jsonl").read_text().splitlines()[0])
    assert trace["policy_version"] == "priority-rule-v1"
    assert trace["source_id"] == "ASM-PRIORITY-RULE-BEHAVIOR-V1"


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


def test_psychology_update_is_pure_bounded_and_keeps_trust_until_outcome_exists() -> None:
    world = make_world()
    agent = world.agents["A001"]
    agent.activity = Activity.RESTING
    agent.needs.toilet_urgency = 0.9995
    agent.needs.hunger = 0.3
    agent.needs.fatigue = 0.001
    agent.stress = 0.999
    agent.trust = 0.2
    view = WorldView.from_world(world)
    state_before = world.canonical_json()
    policy = SimplePsychologyPolicy()

    deltas = policy.update(view, minute=0)
    first_delta = next(delta for delta in deltas if delta.agent_id == "A001")

    assert world.canonical_json() == state_before
    assert deltas == policy.update(view, minute=0)
    assert 0.0 <= first_delta.toilet_urgency <= 1.0
    assert 0.0 <= first_delta.hunger <= 1.0
    assert 0.0 <= first_delta.fatigue <= 1.0
    assert 0.0 <= first_delta.stress <= 1.0
    assert first_delta.fatigue < agent.needs.fatigue
    assert first_delta.trust == agent.trust

    apply_state_deltas(world, deltas)
    world.validate()
    assert world.agents["A001"].needs.fatigue < 0.001
    assert world.agents["A001"].trust == 0.2


def test_psychology_application_rejects_stale_or_duplicate_updates_atomically() -> None:
    world = make_world()
    update = AgentStateDelta(
        agent_id="A001",
        minute_index=0,
        toilet_urgency=0.1,
        hunger=0.2,
        fatigue=0.3,
        stress=0.4,
        trust=0.5,
    )
    stale_update = AgentStateDelta(
        agent_id="A002",
        minute_index=1,
        toilet_urgency=0.1,
        hunger=0.2,
        fatigue=0.3,
        stress=0.4,
        trust=0.5,
    )
    initial = world.canonical_json()

    with pytest.raises(StateInvariantError, match="different minute"):
        apply_state_deltas(world, (update, stale_update))
    assert world.canonical_json() == initial

    with pytest.raises(StateInvariantError, match="duplicate psychology update"):
        apply_state_deltas(world, (update, update))
    assert world.canonical_json() == initial
