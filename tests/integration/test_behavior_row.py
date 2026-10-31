"""The proposals of rules/behavior.rs: every field the bits of policies/behavior.py.

The Python rules stay the reference (docs/rust-port/lanes/h-behavior.md). The bits over whole
scenarios are compared by benchmarks/rule_shadow; here, on values no scenario reaches.
"""

from __future__ import annotations

import copy
import random
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from support.paths import SMALL_REPLAY

from sheltersim import native
from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.body_state import create_body_state
from sheltersim.domain.models import (
    ActiveTask,
    Activity,
    SupportRequestState,
    SupportRequestStatus,
)
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.phases.propose import ProposePhase, _agent_view_snapshot
from sheltersim.experiments.runner import OfflineSimulationController, run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import _MetricSink
from sheltersim.policies.behavior import (
    BehaviorDecisionRunner,
    PriorityRuleBehaviorPolicy,
    UtilityBehaviorPolicy,
    WorldView,
)

pytestmark = pytest.mark.skipif(not native.available(), reason="only with the Rust kernels")


def _exact(value: Any) -> Any:
    """Equal only with the same types, the same bits, and the same order of each dict's keys."""
    if value is None or type(value) is bool:
        return value
    if type(value) is float:
        return value.hex()
    if isinstance(value, Enum):
        return [type(value).__name__, value.value]
    if type(value) is int or type(value) is str:
        return [type(value).__name__, value]
    if isinstance(value, (tuple, list)):
        return [type(value).__name__, *(_exact(item) for item in value)]
    if isinstance(value, dict):
        return ["dict", *([key, _exact(item)] for key, item in value.items())]
    if hasattr(value, "__dataclass_fields__"):
        return [type(value).__name__, *([k, _exact(v)] for k, v in value.__dict__.items())]
    raise TypeError(type(value))


def _rules() -> Any:
    return native.rules("behavior")


def _controller(world: WorldState) -> OfflineSimulationController:
    return OfflineSimulationController(world, _MetricSink(MetricsRecorder()))


def _view(world: WorldState, controller: Any = None) -> WorldView:
    controller = controller or _controller(world)
    return native.rules("world_view").view_for_proposals(controller, world, world.minute_index)


def _both(policy: Any, view: WorldView, seed: int = 11) -> tuple[Any, ...]:
    """Each way of making the proposals, which must be the same; the kernel's made them."""
    runner = BehaviorDecisionRunner(policy)
    made = _rules().propose_due(SimpleNamespace(decisions=runner), view, CounterRNG(seed))
    assert _exact(made) == _exact(runner.propose_due(view, CounterRNG(seed)))
    return made


def _draw_world(draws: random.Random, scenario: Any) -> tuple[WorldState, WorldView]:
    """A world with values spread over each threshold of the rule, and most of it due, and its
    view. The values are the rule's to read and need not make a world the validators accept, so
    the controller is made before them."""
    world = WorldState.from_scenario(scenario)
    world.model_state.setdefault("body_state_v1", create_body_state(world))
    controller = _controller(world)
    world.minute_index = draws.randrange(0, 2880)

    def value() -> float:
        return draws.choice((0.0, 0.25, 0.5, 0.65, 0.8, 0.9, 1.0, draws.random(), draws.random()))

    people = sorted(world.agents)
    records = world.model_state["body_state_v1"]["agents"]
    groups = (None, "", "G1", "G2")
    context: dict[str, Any] = {}
    for agent_id in people:
        agent = world.agents[agent_id]
        agent.needs.toilet_urgency = value()
        agent.needs.hunger = value()
        agent.needs.fatigue = value()
        agent.needs.hydration_need = value()
        agent.stress = value()
        agent.trust = value()
        agent.next_decision_at = draws.choice((0, 0, 0, world.minute_index + 1))
        agent.activity = draws.choice(
            (Activity.IDLE, Activity.RESTING, Activity.SLEEPING, Activity.MOVING)
        )
        record = records[agent_id]
        record["sleep_debt_minutes"] = draws.choice((0.0, 240.0, draws.random() * 900.0))
        for name in ("uncertainty", "reassurance", "discontent", "negative_affect", "hygiene_need"):
            record[name] = value()
        if draws.random() < 0.5:
            others = draws.sample(people, min(9, len(people)))
            context[agent_id] = {
                "care_for_ids": others[:3] if draws.random() < 0.5 else [],
                "caregiver_ids": others[3:6] if draws.random() < 0.5 else [],
                "parent_ids": others[6:] if draws.random() < 0.5 else [],
                "usual_wake_minute": draws.randrange(0, 1440),
                "usual_sleep_minute": draws.randrange(0, 1440),
                "required_sleep_minutes": draws.choice((60, 480, 600)),
            }
    world.model_state["population_presence_v1"] = {
        "people": {
            agent_id: {"household_id": draws.choice(groups)}
            for agent_id in people
            if draws.random() < 0.7
        }
    }
    if context:
        configs = world.model_configs.setdefault("population_context", {"people": {}})
        configs["people"] = context
    zones = sorted(world.zones)
    environment = world.model_state.get("zone_environment_v1")
    if isinstance(environment, dict):
        for number, action in enumerate(("sleep", "rest", "wait", "sleep")):
            if draws.random() < 0.5:
                environment["modifiers"][f"m{number}"] = {
                    "zone_id": draws.choice(zones),
                    "start_minute": 0,
                    "op": "modify_demand_rate",
                    "parameters": {
                        "action": action,
                        "probability_multiplier": draws.choice((0.5, 1.0, 1.5, 2.0)),
                        "duration_multiplier": (
                            draws.choice((0.5, 1.0, 1.25, 2.0))
                            if action in ("rest", "sleep")
                            else 1.0
                        ),
                    },
                    "start_event_id": f"e{number}",
                    "source_id": "s",
                    "assumption_ids": ["ASM-A", "ASM-B"][: number % 3],
                }
    return world, _view(world, controller)


def _draw_policy(draws: random.Random, ids: list[str]) -> Any:
    def weight() -> float:
        return draws.choice((0.0, 0.25, 1, 1.0, draws.random()))

    autonomy = draws.random() < 0.7
    parameters: dict[str, Any] = {
        "temperature": draws.choice((0.2, 0.05, 1, 3.0, draws.random() + 0.01)),
        "decision_interval_minutes": draws.choice((1, 15, 30)),
        "decision_interval_minutes_by_agent": {ids[0]: 7} if draws.random() < 0.5 else {},
        "temperature_by_agent": {ids[1]: draws.choice((0.35, 2))} if draws.random() < 0.5 else {},
        "sleep_debt_utility_weight": draws.choice((1.0, 0.5, draws.random())),
        "resource_service_enabled": draws.random() < 0.7,
        "hydration_action_threshold": draws.choice((0.25, 0.30000000000000004, 1e-07, 1)),
        "hunger_action_threshold": draws.choice((0.7, 0.123456789, 1.0)),
        "water_utility_base": weight(),
        "water_utility_need_weight": weight(),
        "meal_utility_base": weight(),
        "meal_utility_need_weight": weight(),
        "information_query_threshold": draws.choice((1.1, 0.5, 1, 0.123456789)),
        "information_query_utility_weight": weight(),
        "autonomy_enabled": autonomy,
    }
    version = draws.choice(
        (
            "utility-softmax-v1",
            "sleep-aware-utility-softmax-v2",
            "sleep-aware-utility-softmax-v2-autonomy-v2",
            'quote"\\version-é',
        )
    )
    scorer = UtilityBehaviorPolicy(**parameters, policy_version=version)
    if draws.random() < 0.3:
        return PriorityRuleBehaviorPolicy(
            scorer, policy_version=draws.choice((version, "priority-rule-v1"))
        )
    return scorer


@pytest.mark.parametrize("seed", range(30))
def test_each_proposal_of_a_drawn_world_has_the_bits_of_python(
    seed: int, generated_scenario: Any
) -> None:
    draws = random.Random(seed)
    # The generated scenario has the water and meal actions, the small one doesn't.
    scenario = ScenarioSpec.load(SMALL_REPLAY) if seed % 3 == 0 else generated_scenario
    world, view = _draw_world(draws, scenario)
    assert view.resource_service_enabled is (seed % 3 != 0)
    for _ in range(4):
        policy = _draw_policy(draws, sorted(world.agents))
        _both(policy, view, seed=draws.randrange(0, 2**40))
    for agent_id in world.agents:
        rust = _rules().observed_state(SimpleNamespace(), view, agent_id)
        assert _exact(rust) == _exact(_agent_view_snapshot(view.agents[agent_id]))


def test_requests_cooldowns_and_queued_tasks_are_read_as_python_reads_them(
    generated_scenario: Any,
) -> None:
    """No scenario of the gate has someone due with a request or a queued task, so these are here."""
    world = WorldState.from_scenario(generated_scenario)
    controller = _controller(world)
    _everyone_due(world)
    people = sorted(world.agents)
    world.minute_index = 40
    for number, status in enumerate(SupportRequestStatus):
        request = SupportRequestState(f"R{number}", people[number], 5, 10, status=status)
        world.support_requests[request.id] = request
        world.agents[people[number]].support_request_id = request.id
        # Enough for the help candidate but for the request.
        world.agents[people[number]].needs.fatigue = 0.9
    world.agents[people[4]].support_request_id = "nothing"
    world.agents[people[5]].support_cooldown_until = 55
    world.agents[people[6]].support_cooldown_until = 25
    world.active_tasks["T1"] = ActiveTask("T1", "toilet_queue", people[7], "F1", 10, 90)
    world.active_tasks["T2"] = ActiveTask("T2", "resource_service_queue", people[8], None, 50, 90)
    world.active_tasks["T3"] = ActiveTask("T3", "toilet_queue", people[9], "F1", 20, 90)
    world.active_tasks["T4"] = ActiveTask("T4", "resource_service_queue", people[9], "F2", 30, 90)
    view = _view(world, controller)
    for threshold in (1.1, 0.0):
        policy = UtilityBehaviorPolicy(information_query_threshold=threshold)
        made = _both(policy, view)
        assert {proposal.agent_id for proposal in made} == set(people)
    snapshots = {}
    for agent_id in people:
        snapshots[agent_id] = _rules().observed_state(SimpleNamespace(), view, agent_id)
        assert _exact(snapshots[agent_id]) == _exact(_agent_view_snapshot(view.agents[agent_id]))
    assert snapshots[people[0]]["support"]["status"] == "pending"
    assert snapshots[people[0]]["support"]["wait_minutes"] == 35
    assert snapshots[people[4]]["support"]["request_id"] == "nothing"
    assert snapshots[people[5]]["support"]["cooldown_remaining_minutes"] == 15
    assert snapshots[people[6]]["support"]["cooldown_remaining_minutes"] == 0
    assert snapshots[people[7]]["queue"]["wait_minutes"] == 30
    assert snapshots[people[8]]["queue"] == {
        "task_id": "T2",
        "kind": "resource_service_queue",
        "target_id": None,
        "wait_minutes": 0,
    }
    # The first of two queued tasks: the order they were added in.
    assert snapshots[people[9]]["queue"]["task_id"] == "T3"


def test_drawn_worlds_reach_what_they_are_for(generated_scenario: Any) -> None:
    """The comparison above means little if the branches stay unreached."""
    actions: set[str] = set()
    with_target: set[str] = set()
    demand = ties = 0
    for seed in range(60):
        draws = random.Random(seed)
        world, view = _draw_world(draws, generated_scenario)
        policy = _draw_policy(draws, sorted(world.agents))
        for proposal in _both(policy, view):
            actions.add(proposal.action.value)
            if proposal.target_id is not None:
                with_target.add(proposal.action.value)
            ties += proposal.rng_key[-1] == "priority-tie-break"
            demand += any("experimental zone" in item.trigger for item in proposal.candidates)
    assert actions >= {
        "wait",
        "request_toilet",
        "rest",
        "sleep",
        "seek_help",
        "seek_information",
        "socialize",
        "care_family",
        "help_others",
        "fetch_water",
        "collect_meal",
    }
    assert with_target == {"socialize", "care_family", "help_others"}
    assert ties > 0 and demand > 0


def test_each_proposal_of_a_run_has_the_bits_of_python(
    generated_scenario: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = copy.deepcopy(generated_scenario.model_dump(mode="json"))
    document["duration_minutes"] = 90
    counts = {"calls": 0, "proposals": 0, "observed": 0}

    def propose(self: Any, view: WorldView, rng: Any) -> Any:
        made = _rules().propose_due(self, view, rng)
        assert _exact(made) == _exact(self.decisions.propose_due(view, rng))
        counts["calls"] += 1
        counts["proposals"] += len(made)
        return made

    def observed(self: Any, view: WorldView, agent_id: str) -> Any:
        made = _rules().observed_state(self, view, agent_id)
        assert _exact(made) == _exact(_agent_view_snapshot(view.agents[agent_id]))
        counts["observed"] += 1
        return made

    monkeypatch.setattr(OfflineSimulationController, "_propose_due", propose)
    monkeypatch.setattr(OfflineSimulationController, "_observed_state", observed)
    run_scenario(ScenarioSpec.model_validate(document), tmp_path, trace_level=TraceLevel.OFF)
    assert counts["calls"] == 90
    assert counts["proposals"] == counts["observed"] > 0


def test_a_tick_makes_no_python_view_of_the_people(
    generated_scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    controller = _controller(world)
    engine = TickEngine(world, phase_handler=controller)
    views: list[WorldView] = []
    apply_due = OfflineSimulationController._apply_due_proposals
    proposals: list[int] = []
    propose = OfflineSimulationController._propose_due

    def counted(self: Any, view: WorldView, rng: Any) -> Any:
        made = propose(self, view, rng)
        proposals.append(len(made))
        return made

    def checked(self: Any, view: WorldView, rng: Any) -> None:
        apply_due(self, view, rng)
        views.append(view)

    monkeypatch.setattr(OfflineSimulationController, "_propose_due", counted)
    monkeypatch.setattr(OfflineSimulationController, "_apply_due_proposals", checked)
    for _ in range(20):
        engine.step()
    assert len(views) == 20 and sum(proposals) > 0
    # The first tick's social step is Python's: its namespace doesn't exist yet.
    for view in views[1:]:
        assert "agents" not in view.__dict__


def test_a_view_python_made_is_left_to_python(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    world.model_state.setdefault("body_state_v1", create_body_state(world))
    view = WorldView.from_world(world)
    assert "_rust_view" not in view.__dict__
    controller = SimpleNamespace(decisions=BehaviorDecisionRunner())
    with pytest.raises(native.Unsupported, match="Python's"):
        _rules().propose_due(controller, view, CounterRNG(1))
    with pytest.raises(native.Unsupported, match="Python's"):
        _rules().observed_state(controller, view, "A001")


def test_a_view_without_its_hash_is_left_to_python(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    controller = _controller(world)
    view = native.rules("world_view").view_after_movement(controller, world)
    assert view.state_hash is None
    with pytest.raises(native.Unsupported, match="hash"):
        _rules().propose_due(
            SimpleNamespace(decisions=BehaviorDecisionRunner()), view, CounterRNG(1)
        )
    with pytest.raises(ValueError, match="state hash"):
        BehaviorDecisionRunner().propose_due(view, CounterRNG(1))


def test_what_the_kernel_does_not_know_is_left_to_python(generated_scenario: Any) -> None:
    class OtherPolicy(UtilityBehaviorPolicy):
        pass

    class OtherRunner(BehaviorDecisionRunner):
        pass

    class OtherRandom(CounterRNG):
        pass

    world = WorldState.from_scenario(generated_scenario)
    view = _view(world)
    rng = CounterRNG(1)
    cases = (
        (SimpleNamespace(decisions=OtherRunner()), rng, "decisions"),
        (SimpleNamespace(decisions=BehaviorDecisionRunner(OtherPolicy())), rng, "policy"),
        (
            SimpleNamespace(
                decisions=BehaviorDecisionRunner(PriorityRuleBehaviorPolicy(OtherPolicy()))
            ),
            rng,
            "scorer",
        ),
        (SimpleNamespace(decisions=BehaviorDecisionRunner()), OtherRandom(1), "RNG"),
    )
    for controller, source, reason in cases:
        with pytest.raises(native.Unsupported, match=reason):
            _rules().propose_due(controller, view, source)
    with pytest.raises(native.Unsupported, match="no such person"):
        _rules().observed_state(SimpleNamespace(), view, "nobody")


def _everyone_due(world: WorldState) -> None:
    for agent in world.agents.values():
        agent.activity = Activity.IDLE
        agent.next_decision_at = 0


@pytest.mark.parametrize(
    ("field", "value", "error", "message"),
    [
        # The difference from the best utility, divided by this, leaves the range of exp.
        ("temperature", -1e-300, OverflowError, "range"),
        ("decision_interval_minutes", 0, ValueError, "decision timing"),
    ],
)
def test_what_python_raises_on_is_left_to_python_with_its_text(
    generated_scenario: Any, field: str, value: Any, error: type[Exception], message: str
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    _everyone_due(world)
    world.agents["A001"].needs.fatigue = 0.9
    view = _view(world)
    policy = UtilityBehaviorPolicy()
    object.__setattr__(policy, field, value)
    runner = BehaviorDecisionRunner(policy)
    with pytest.raises(native.Unsupported):
        _rules().propose_due(SimpleNamespace(decisions=runner), view, CounterRNG(1))
    with pytest.raises(error, match=message):
        runner.propose_due(view, CounterRNG(1))


def test_the_step_runs_python_when_the_kernel_refuses(
    generated_scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    _everyone_due(world)
    controller = _controller(world)
    view = _view(world, controller)
    calls: list[str] = []

    class Counting(UtilityBehaviorPolicy):
        def propose(self, *args: Any, **options: Any) -> Any:
            calls.append(args[1])
            return super().propose(*args, **options)

    controller.decisions = BehaviorDecisionRunner(Counting())
    made = controller._propose_due(view, CounterRNG(1))
    assert len(made) == len(world.agents) == len(calls)
    assert _exact(made) == _exact(ProposePhase._propose_due(controller, view, CounterRNG(1)))
    assert "agents" in view.__dict__
