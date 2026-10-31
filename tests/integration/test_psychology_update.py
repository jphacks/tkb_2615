"""Each person's minute in Rust: native/src/rules/psychology.rs against policies/psychology.py.

The Python rules stay the reference (docs/rust-port/lanes/a-psychology.md). The bits over whole
scenarios are compared by benchmarks/rule_shadow; here, on worlds whose values no scenario reaches.
"""

from __future__ import annotations

import copy
import json
import random
from dataclasses import fields, replace
from types import SimpleNamespace
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.body_state import create_body_state
from sheltersim.domain.models import Activity, SupportRequestState, SupportRequestStatus
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.phases.update import UpdatePhase
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink
from sheltersim.policies.alternatives import BoundedStressPolicy
from sheltersim.policies.behavior import WorldView
from sheltersim.policies.psychology import (
    AgentStateDelta,
    IntegratedDistressLifeStatePolicy,
    LifeStatePsychologyPolicy,
    MeanRevertingDistressPolicy,
    PsychologyParameters,
    SimplePsychologyPolicy,
    apply_state_deltas,
)
from sheltersim.policies.registry import BodyDynamicsFormula

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

FULL = BodyDynamicsFormula("body-feedback-complete-v1", "S")
SLEEP_ONLY = BodyDynamicsFormula("body-feedback-sleep-debt-v1", "S")


def _rules() -> Any:
    return native.rules("psychology")


def _controller(world: WorldState, policy: Any, trace: TraceLevel = TraceLevel.OFF) -> Any:
    return SimpleNamespace(
        world=world, psychology=policy, artifacts=SimpleNamespace(trace_level=trace)
    )


def _text(delta: AgentStateDelta) -> str:
    """Float bits and 1 against 1.0 all show."""
    return json.dumps([getattr(delta, item.name) for item in fields(delta)])


def _people_text(world: WorldState) -> str:
    return json.dumps(
        [[agent.stress, agent.trust, agent.needs.to_dict()] for agent in world.agents.values()]
    )


def _draw_parameters(draws: random.Random, ids: list[str]) -> PsychologyParameters:
    def rate() -> float:
        return draws.choice((0.0, draws.random() * 0.25))

    return PsychologyParameters(
        toilet_growth=rate(),
        hunger_growth=rate(),
        awake_fatigue_growth=rate(),
        resting_fatigue_recovery=rate(),
        sleeping_fatigue_recovery=rate(),
        stress_pressure=rate(),
        queued_stress=rate(),
        resting_stress_recovery=rate(),
        sleeping_stress_recovery=rate(),
        need_pressure_threshold=draws.random(),
        hydration_growth=rate(),
        support_wait_stress=rate(),
        natural_recovery_rate_per_minute=rate(),
        toilet_growth_by_agent={ids[0]: rate()},
        hunger_growth_by_agent={ids[0]: rate()},
        awake_fatigue_growth_by_agent={ids[0]: rate()},
        natural_recovery_rate_by_agent={ids[0]: rate()},
        baseline_distress_by_agent={ids[0]: draws.random()},
    )


def _draw_policies(draws: random.Random, ids: list[str]) -> list[Any]:
    body = {
        "uncertainty_drift_per_minute": draws.random() * 0.01,
        "discontent_stress_rate": draws.random() * 0.01,
        "sleep_debt_stress_rate_by_agent": {ids[0]: draws.random() * 0.01},
        "negative_affect_stress_rate_by_agent": {ids[0]: draws.random() * 0.01},
        "discontent_stress_rate_by_agent": {ids[0]: draws.random() * 0.01},
    }
    parameters = _draw_parameters(draws, ids)
    return [
        SimplePsychologyPolicy(parameters),
        SimplePsychologyPolicy(parameters, include_direct_rest_stress_recovery=False),
        BoundedStressPolicy(parameters),
        MeanRevertingDistressPolicy(parameters),
        LifeStatePsychologyPolicy(parameters, body_config=body),
        LifeStatePsychologyPolicy(parameters, body_config=body, body_formula=FULL),
        LifeStatePsychologyPolicy(parameters, body_config=body, body_formula=SLEEP_ONLY),
        LifeStatePsychologyPolicy(
            parameters,
            body_config=body,
            body_formula=FULL,
            include_direct_rest_stress_recovery=False,
            use_tick_start_stress_for_feedback=True,
        ),
        IntegratedDistressLifeStatePolicy(parameters, body_config=body),
        IntegratedDistressLifeStatePolicy(parameters, body_config=body, body_formula=FULL),
        IntegratedDistressLifeStatePolicy(parameters, body_config=body, body_formula=SLEEP_ONLY),
    ]


def _shuffled_world(scenario: Any, draws: random.Random) -> WorldState:
    world = WorldState.from_scenario(scenario)
    world.model_state.setdefault("body_state_v1", create_body_state(world))
    zones = sorted(world.zones)
    extremes = (0.0, 1.0, 0.5, 0.35, 0.7)
    for number, agent in enumerate(world.agents.values()):
        agent.activity = draws.choice(list(Activity))
        agent.zone_id = draws.choice(zones)
        needs = agent.needs
        for name in ("toilet_urgency", "hunger", "fatigue", "hydration_need"):
            setattr(needs, name, draws.random() if number % 3 else draws.choice(extremes))
        agent.stress = draws.random() if number % 3 else draws.choice(extremes)
        record = world.model_state["body_state_v1"]["agents"][agent.id]
        for name in record:
            record[name] = draws.random() if number % 3 else draws.choice(extremes)
        record["sleep_debt_minutes"] = draws.choice((0.0, 1440.0, draws.random() * 1440.0))
    for number, agent in enumerate(list(world.agents.values())[:6]):
        status = list(SupportRequestStatus)[number % 4]
        request = SupportRequestState(f"R{number}", agent.id, 0, 10, status=status)
        world.support_requests[request.id] = request
        agent.support_request_id = request.id
    # An id with no request is no open request.
    list(world.agents.values())[7].support_request_id = "nothing"
    return world


def _same_as_python(world: WorldState, policy: Any, view: Any) -> None:
    result = _rules().update_psychology(_controller(world, policy), view, world.minute_index)
    assert result.traces == ()
    python = policy.explain_update(view, world.minute_index).deltas
    assert [_text(delta) for delta in result.deltas] == [_text(delta) for delta in python]
    for delta in result.deltas:
        assert type(delta) is AgentStateDelta
        # What the social step does to a delta.
        assert delta.with_stress(0.25).stress == 0.25
    if isinstance(policy, IntegratedDistressLifeStatePolicy):
        fast = policy.deltas_without_traces(view, world.minute_index)
        assert [_text(delta) for delta in result.deltas] == [_text(delta) for delta in fast]


@pytest.mark.parametrize("seed", range(8))
def test_every_model_gives_the_bits_of_python(generated_scenario: Any, seed: int) -> None:
    draws = random.Random(seed)
    world = _shuffled_world(generated_scenario, draws)
    view = WorldView.from_world(world, include_state_hash=False)
    for agent in world.agents.values():
        assert view.agents[agent.id].needs.hunger == agent.needs.hunger
    for policy in _draw_policies(draws, sorted(world.agents)):
        _same_as_python(world, policy, view)


def test_people_of_a_group_are_separated_as_python_says(generated_scenario: Any) -> None:
    draws = random.Random(5)
    world = _shuffled_world(generated_scenario, draws)
    view = WorldView.from_world(world, include_state_hash=False)
    separated = {
        agent.id
        for agent in view.agents.values()
        if any(
            peer != agent.id and view.agents[peer].zone_id != agent.zone_id
            for peer in view.group_members.get(agent.group_id or "", ())
        )
    }
    assert separated and separated != set(view.agents)
    policy = LifeStatePsychologyPolicy(PsychologyParameters(), body_formula=FULL)
    _same_as_python(world, policy, view)


def test_a_one_percent_change_shows(generated_scenario: Any) -> None:
    draws = random.Random(1)
    world = _shuffled_world(generated_scenario, draws)
    view = WorldView.from_world(world, include_state_hash=False)
    parameters = PsychologyParameters()
    changed = replace(parameters, stress_pressure=parameters.stress_pressure * 1.01)
    rust = _rules().update_psychology(
        _controller(world, IntegratedDistressLifeStatePolicy(changed, body_formula=FULL)),
        view,
        world.minute_index,
    )
    python = IntegratedDistressLifeStatePolicy(parameters, body_formula=FULL).explain_update(
        view, world.minute_index
    )
    assert [_text(delta) for delta in rust.deltas] != [_text(delta) for delta in python.deltas]


def _view_with(view: Any, agent_id: str, **changes: Any) -> Any:
    return replace(
        view, agents={**view.agents, agent_id: replace(view.agents[agent_id], **changes)}
    )


class _OwnRules(SimplePsychologyPolicy):
    policy_version = "causal-stress-v1"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"sleep_debt_minutes": -1.0}, "out of bounds"),
        ({"sleep_debt_minutes": 1441.0}, "out of bounds"),
        ({"sleep_debt_minutes": float("nan")}, "out of bounds"),
        ({"sleep_debt_minutes": 0}, "not a float"),
        ({"rest_efficiency_multiplier": 1.5}, "out of bounds"),
        ({"required_sleep_minutes": 1441}, "out of bounds"),
        ({"required_sleep_minutes": 8.0}, "not an int"),
        ({"positive_affect": 3.0}, "out of bounds"),
        ({"reassurance": 2.0}, "out of bounds"),
        ({"group_id": ""}, "empty"),
    ],
)
def test_what_the_python_checks_may_refuse_goes_to_them(
    generated_scenario: Any, change: dict[str, Any], reason: str
) -> None:
    world = _shuffled_world(generated_scenario, random.Random(2))
    before = _people_text(world)
    view = WorldView.from_world(world, include_state_hash=False)
    first = next(iter(view.agents))
    policy = IntegratedDistressLifeStatePolicy(PsychologyParameters(), body_formula=FULL)
    with pytest.raises(native.Unsupported, match=reason):
        _rules().update_psychology(_controller(world, policy), _view_with(view, first, **change), 0)
    assert _people_text(world) == before


@pytest.mark.parametrize(
    ("policy", "trace", "minute", "reason"),
    [
        (SimplePsychologyPolicy(), TraceLevel.SUMMARY, 0, "trace"),
        (SimplePsychologyPolicy(), TraceLevel.FULL, 0, "trace"),
        (_OwnRules(), TraceLevel.OFF, 0, "not one this kernel knows"),
        (object(), TraceLevel.OFF, 0, "not one this kernel knows"),
        (SimplePsychologyPolicy(), TraceLevel.OFF, 1, "not the view's"),
        (
            SimplePsychologyPolicy(PsychologyParameters(toilet_growth=0)),
            TraceLevel.OFF,
            0,
            "not a float",
        ),
        (
            SimplePsychologyPolicy(PsychologyParameters(toilet_growth_by_agent={"A001": 0})),
            TraceLevel.OFF,
            0,
            "not a float",
        ),
        (
            MeanRevertingDistressPolicy(
                PsychologyParameters(baseline_distress_by_agent={"A001": 0})
            ),
            TraceLevel.OFF,
            0,
            "not a float",
        ),
    ],
)
def test_what_the_kernel_does_not_handle_is_unsupported(
    generated_scenario: Any, policy: Any, trace: TraceLevel, minute: int, reason: str
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    view = WorldView.from_world(world, include_state_hash=False)
    with pytest.raises(native.Unsupported, match=reason):
        _rules().update_psychology(_controller(world, policy, trace), view, minute)


def test_a_tainted_store_goes_to_python(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    view = WorldView.from_world(world, include_state_hash=False)
    next(iter(world.agents.values())).stress = 0
    assert world._core.entities().tainted is not None
    with pytest.raises(native.Unsupported, match="tainted"):
        _rules().update_psychology(_controller(world, SimplePsychologyPolicy()), view, 0)
    deltas = SimplePsychologyPolicy().update(view, 0)
    with pytest.raises(native.Unsupported, match="tainted"):
        _rules().apply_deltas(None, world, deltas)
    apply_state_deltas(world, deltas)


def test_a_trust_out_of_bounds_keeps_the_python_error(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    next(iter(world.agents.values())).trust = 1.5
    view = WorldView.from_world(world, include_state_hash=False)
    policy = SimplePsychologyPolicy()
    with pytest.raises(native.Unsupported, match="trust"):
        _rules().update_psychology(_controller(world, policy), view, 0)
    with pytest.raises(ValueError, match="trust"):
        policy.update(view, 0)


# ---- Writing the values ----------------------------------------------------------------------


def _deltas_of(world: WorldState, draws: random.Random, *, body: bool) -> tuple[Any, ...]:
    def value() -> float:
        return draws.random()

    return tuple(
        AgentStateDelta(
            agent_id=agent_id,
            minute_index=world.minute_index,
            toilet_urgency=value(),
            hunger=agent.needs.hunger,
            fatigue=value(),
            stress=value(),
            trust=agent.trust,
            hydration_need=value(),
            **(
                {
                    "sleep_debt_minutes": value() * 1440.0,
                    "uncertainty": value(),
                    "reassurance": value(),
                    "discontent": value(),
                    "positive_affect": value(),
                    "negative_affect": value(),
                    "hygiene_need": value(),
                }
                if body
                else {}
            ),
        )
        for agent_id, agent in world.agents.items()
    )


@pytest.mark.parametrize("body", [False, True])
def test_writing_gives_what_the_python_rule_gives(generated_scenario: Any, body: bool) -> None:
    fast = WorldState.from_scenario(generated_scenario)
    fast.model_state.setdefault("body_state_v1", create_body_state(fast))
    slow = copy.deepcopy(fast)
    deltas = _deltas_of(fast, random.Random(3), body=body)
    store = fast._core.entities()
    forwards = store.stats()["forwards"]
    _rules().apply_deltas(None, fast, deltas)
    # Nothing came back through __setattr__, and nothing but the objects and rows moved.
    assert store.stats()["forwards"] == forwards
    apply_state_deltas(slow, deltas)
    assert _people_text(fast) == _people_text(slow)
    assert json.dumps(fast.model_state["body_state_v1"]) == json.dumps(
        slow.model_state["body_state_v1"]
    )
    assert fast.canonical_json() == slow.canonical_json()
    assert store.verify(fast) is None
    # The objects still forward what comes next, to the same rows.
    first = next(iter(fast.agents.values()))
    first.needs.hunger = 0.125
    first.stress = 0.25
    assert store.verify(fast) is None


def test_an_unchanged_world_is_not_written_again(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    world.model_state.setdefault("body_state_v1", create_body_state(world))
    deltas = _deltas_of(world, random.Random(3), body=False)
    _rules().apply_deltas(None, world, deltas)
    agent = next(iter(world.agents.values()))
    stress = agent.stress
    again = tuple(replace(delta, stress=float(delta.stress)) for delta in deltas)
    _rules().apply_deltas(None, world, again)
    assert agent.stress is stress


def _broken(delta: AgentStateDelta, **changes: Any) -> AgentStateDelta:
    copied = copy.copy(delta)
    for name, value in changes.items():
        object.__setattr__(copied, name, value)
    return copied


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"agent_id": "nobody"}, "references unknown Agent"),
        ({"minute_index": 9}, "computed for a different minute"),
        ({"hunger": float("nan")}, "invalid psychology update"),
        ({"hunger": 1.5}, "invalid psychology update"),
        ({"stress": -0.5}, "invalid psychology update"),
        ({"trust": True}, "invalid psychology update"),
    ],
)
def test_a_bad_delta_changes_nothing_and_python_gives_its_text(
    generated_scenario: Any, change: dict[str, Any], message: str
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    world.model_state.setdefault("body_state_v1", create_body_state(world))
    deltas = list(_deltas_of(world, random.Random(4), body=True))
    deltas[-1] = _broken(deltas[-1], **change)
    before = world.canonical_json()
    with pytest.raises(native.Unsupported):
        _rules().apply_deltas(None, world, tuple(deltas))
    assert world.canonical_json() == before
    assert world._core.entities().verify(world) is None
    with pytest.raises(StateInvariantError, match=message):
        apply_state_deltas(world, tuple(deltas))


def test_two_deltas_for_one_person_keep_the_python_error(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    delta = _deltas_of(world, random.Random(4), body=False)[0]
    before = world.canonical_json()
    with pytest.raises(native.Unsupported, match="two deltas"):
        _rules().apply_deltas(None, world, (delta, delta))
    assert world.canonical_json() == before
    with pytest.raises(StateInvariantError, match="duplicate"):
        apply_state_deltas(world, (delta, delta))


@pytest.mark.parametrize("drop", ["namespace", "record"])
def test_body_values_without_a_record_keep_the_python_error(
    generated_scenario: Any, drop: str
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    world.model_state.setdefault("body_state_v1", create_body_state(world))
    deltas = _deltas_of(world, random.Random(4), body=True)
    if drop == "namespace":
        del world.model_state["body_state_v1"]
    else:
        del world.model_state["body_state_v1"]["agents"][deltas[-1].agent_id]
    before = _people_text(world)
    with pytest.raises(native.Unsupported):
        _rules().apply_deltas(None, world, deltas)
    assert _people_text(world) == before
    with pytest.raises(StateInvariantError, match="require initialized body_state_v1"):
        apply_state_deltas(world, deltas)


def test_a_delta_of_ints_is_left_to_python_which_accepts_it(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    delta = _broken(_deltas_of(world, random.Random(4), body=False)[0], hunger=1)
    with pytest.raises(native.Unsupported):
        _rules().apply_deltas(None, world, (delta,))
    apply_state_deltas(world, (delta,))
    assert world.agents[delta.agent_id].needs.hunger == 1


def test_a_tracked_body_namespace_is_left_to_python(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    world.model_state["body_state_v1"] = type("OtherDict", (dict,), {})(create_body_state(world))
    deltas = _deltas_of(world, random.Random(4), body=True)
    with pytest.raises(native.Unsupported, match="not a plain dict"):
        _rules().apply_deltas(None, world, deltas)


# ---- Whole ticks ---------------------------------------------------------------------------------


def _counted_ticks(scenario: Any, minutes: int) -> tuple[WorldState, dict[str, int]]:
    world = WorldState.from_scenario(scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    counts = {"update": 0, "apply": 0}
    rules = _rules()
    update, apply = rules.update_psychology, rules.apply_deltas

    def counted_update(view: Any, minute: int) -> Any:
        counts["update"] += 1
        return update(controller, view, minute)

    def counted_apply(target: Any, deltas: Any) -> None:
        counts["apply"] += 1
        apply(controller, target, deltas)

    # An Unsupported here fails the test instead of running the Python step.
    controller._psychology_update = counted_update
    controller._apply_state_deltas = counted_apply
    for _ in range(minutes):
        engine.step()
    return world, counts


def test_ticks_run_in_the_kernels_and_make_the_python_world(
    generated_scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast, counts = _counted_ticks(generated_scenario, 40)
    assert counts == {"update": 40, "apply": 40}
    assert fast._core.entities().verify(fast) is None
    for step in ("_psychology_update", "_apply_state_deltas", "_update_long_term_psychology"):
        monkeypatch.setattr(OfflineSimulationController, step, getattr(UpdatePhase, step))
    slow = WorldState.from_scenario(generated_scenario)
    controller = OfflineSimulationController(slow, _MetricSink(MetricsRecorder()))
    engine = TickEngine(slow, phase_handler=controller)
    for _ in range(40):
        engine.step()
    assert slow.canonical_json() == fast.canonical_json()
