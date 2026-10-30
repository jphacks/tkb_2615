"""The Rust kernels (feature/rust-port, native/) give the same values as the Python
code they replace, bit for bit, at every tick of a generated Scenario.

Skipped when the sheltersim_native extension is not installed; CI on the Rust port
branch builds and installs it.
"""

from __future__ import annotations

import json
import struct

import pytest

from sheltersim import native
from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec

pytestmark = pytest.mark.skipif(
    not native.available(), reason="the sheltersim_native extension is not installed"
)


def _same_float_bits(x: object, y: object) -> bool:
    if isinstance(x, float) and isinstance(y, float):
        return struct.pack("<d", x) == struct.pack("<d", y)
    return x == y and type(x) is type(y)


def test_the_psychology_kernel_gives_the_python_deltas(
    generated_scenario, tmp_path, monkeypatch
) -> None:
    from sheltersim.policies.psychology import IntegratedDistressLifeStatePolicy

    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 360
    scenario = ScenarioSpec.model_validate(document)
    compared = []
    kernel = IntegratedDistressLifeStatePolicy.native_deltas

    def checked(self, view, minute):
        rust = kernel(self, view, minute)
        python = self.deltas_without_traces(view, minute)
        assert rust is not None and len(rust) == len(python)
        for a, b in zip(rust, python, strict=True):
            for name in a.__dataclass_fields__:
                assert _same_float_bits(getattr(a, name), getattr(b, name)), (
                    minute,
                    a.agent_id,
                    name,
                )
        compared.append(minute)
        return rust

    monkeypatch.setattr(IntegratedDistressLifeStatePolicy, "native_deltas", checked)
    run_scenario(scenario, tmp_path, trace_level=TraceLevel.OFF)
    assert compared == list(range(360))


def test_the_psychology_kernel_leaves_out_of_bounds_values_to_python(generated_scenario) -> None:
    """AgentStateDelta's checks are skipped only for values known to be within bounds."""
    from dataclasses import replace

    from sheltersim.core.world import WorldState
    from sheltersim.domain.body_state import create_body_state
    from sheltersim.policies.behavior import WorldView
    from sheltersim.policies.psychology import IntegratedDistressLifeStatePolicy
    from sheltersim.policies.registry import create_psychology

    world = WorldState.from_scenario(generated_scenario)
    world.model_state.setdefault("body_state_v1", create_body_state(world))
    policy = create_psychology(world.model_configs, resource_service_enabled=True)
    assert isinstance(policy, IntegratedDistressLifeStatePolicy)
    view = WorldView.from_world(world, include_state_hash=False)
    assert policy.native_deltas(view, view.minute_index) is not None
    first = next(iter(view.agents))

    def changed(**fields):
        agents = {**view.agents, first: replace(view.agents[first], **fields)}
        return replace(view, agents=agents)

    assert policy.native_deltas(changed(trust=1), view.minute_index) is None
    broken = changed(trust=1.5)
    assert policy.native_deltas(broken, view.minute_index) is None
    with pytest.raises(ValueError):
        policy.deltas_without_traces(broken, view.minute_index)


def _outcome(validate, world) -> str:
    try:
        validate(world)
    except ValueError as error:
        return str(error)
    return "valid"


def test_the_social_relationship_check_judges_every_record_as_python_does(
    generated_scenario, monkeypatch
) -> None:
    """Each record and key, valid or broken in any field, gets the same outcome (the
    same first error) with the Rust check in front of the Python loop as from the loop."""
    from enum import IntEnum

    from sheltersim.core.world import WorldState
    from sheltersim.domain import social_network
    from sheltersim.domain.social_network import (
        SOCIAL_NETWORK_STATE_KEY,
        create_social_network_state,
        validate_social_network_state,
    )

    class Count(IntEnum):
        THREE = 3

    record = {
        "contact_count": 1,
        "conversation_count": 0,
        "cooperation_count": 0,
        "trust": 0.5,
        "shared_message_ids": [],
        "last_contact_minute": 3,
        "last_interaction": None,
    }
    changes = [
        {},
        {"contact_count": -1},
        {"contact_count": 10**30},
        {"contact_count": -(10**30)},
        {"conversation_count": True},
        {"cooperation_count": 1.0},
        {"cooperation_count": Count.THREE},
        {"shared_message_ids": ()},
        {"trust": None},
        {"trust": 1},
        {"trust": 2},
        {"trust": True},
        {"trust": float("nan")},
        {"trust": 1.0000001},
        {"trust": "0.5"},
        {"last_contact_minute": None},
        {"last_contact_minute": -1},
        {"last_contact_minute": False},
        {"last_contact_minute": 2.0},
        {"extra": 1},
    ]
    candidates = [{**record, **change} for change in changes]
    missing = dict(record)
    del missing["trust"]
    candidates += [missing, ["not", "a", "dict"]]
    keys = ['["A001","A002"]', '["A001","A001"]', '["A001", "A002"]', "[", 7, None]
    world = WorldState.from_scenario(generated_scenario)
    world.model_state[SOCIAL_NETWORK_STATE_KEY] = create_social_network_state()
    relationships = world.model_state[SOCIAL_NETWORK_STATE_KEY]["relationships"]
    cases = [{key: record} for key in keys] + [
        {'["A001","A002"]': candidate} for candidate in candidates
    ]
    # Several records: the first broken one decides, as in the Python loop.
    cases.append({'["A001","A003"]': record, '["A002","A003"]': candidates[1], "[": record})
    for case in cases:
        relationships.clear()
        relationships.update(case)
        rust = _outcome(validate_social_network_state, world)
        with monkeypatch.context() as patch:
            patch.setattr(social_network.native, "social_relationships_valid", lambda *a: False)
            python = _outcome(validate_social_network_state, world)
        assert rust == python, case


def test_the_body_relationship_check_judges_every_record_as_python_does(
    generated_scenario, monkeypatch
) -> None:
    """Each body_state_v1 relationship, valid or broken, gets the same outcome from the
    Rust check as from the Python loop, including keys naming people who left."""
    from enum import IntEnum

    from sheltersim.core.world import WorldState
    from sheltersim.domain import body_state
    from sheltersim.domain.body_state import (
        BODY_STATE_KEY,
        create_body_state,
        validate_body_state,
    )

    class Count(IntEnum):
        TWO = 2

    world = WorldState.from_scenario(generated_scenario)
    world.model_state[BODY_STATE_KEY] = create_body_state(world)
    first, second = sorted(world.agents)[:2]
    record = {
        "contact_count": 1,
        "trust": 0.5,
        "contact_minutes": 2,
        "last_contact_minute": 0,
        "last_interaction": None,
        "topic_ids": [],
    }
    changes = [
        {},
        {"contact_count": -1},
        {"contact_count": True},
        {"contact_count": Count.TWO},
        {"contact_minutes": 2.0},
        {"contact_minutes": 10**30},
        {"topic_ids": ("a",)},
        {"topic_ids": ["a"] * 32},
        {"topic_ids": ["a"] * 33},
        {"topic_ids": ["a" * 64, "b" * 65]},
        {"topic_ids": [1]},
        {"trust": None},
        {"trust": 1},
        {"trust": -0.1},
        {"trust": False},
        {"last_contact_minute": None},
        {"last_contact_minute": Count.TWO},
        {"last_contact_minute": -3},
        {"extra": 1},
    ]
    candidates = [{**record, **change} for change in changes] + [["not", "a", "dict"]]
    keys = [
        f'["{first}","{second}"]',
        f'["{first}","{first}"]',
        f'["{first}","nobody"]',
        f'[ "{first}","{second}"]',
        f'["{first}"]',
        "[",
        7,
    ]
    state = world.model_state[BODY_STATE_KEY]
    cases = [{key: record} for key in keys] + [
        {f'["{first}","{second}"]': candidate} for candidate in candidates
    ]
    for case in cases:
        state["relationships"] = dict(case)
        rust = _outcome(validate_body_state, world)
        with monkeypatch.context() as patch:
            patch.setattr(body_state.native, "body_relationships_valid", lambda *a: False)
            python = _outcome(validate_body_state, world)
        assert rust == python, case


def _world_with_extensions(generated_scenario):
    from sheltersim.core.world import WorldState

    return WorldState.from_scenario(generated_scenario)


def _same_outcomes(world, validate, native_name, monkeypatch, record, changes, write) -> None:
    """Each changed record gets the same outcome with the Rust check as without it."""
    from sheltersim import native as native_module

    original = dict(record)
    for change in changes:
        write({**original, **change})
        rust = _outcome(validate, world)
        with monkeypatch.context() as patch:
            patch.setattr(native_module, native_name, lambda *a: False)
            python = _outcome(validate, world)
        assert rust == python, change
    write(["not", "a", "dict"])
    rust = _outcome(validate, world)
    with monkeypatch.context() as patch:
        patch.setattr(native_module, native_name, lambda *a: False)
        python = _outcome(validate, world)
    assert rust == python
    missing = dict(original)
    missing.popitem()
    write(missing)
    rust = _outcome(validate, world)
    with monkeypatch.context() as patch:
        patch.setattr(native_module, native_name, lambda *a: False)
        python = _outcome(validate, world)
    assert rust == python
    write({**original, "extra": 1})
    rust = _outcome(validate, world)
    assert rust != "valid"
    write(original)
    assert _outcome(validate, world) == "valid"


_NUMBERS = [
    0,
    1,
    2,
    -1,
    0.5,
    1.0,
    1.5,
    -0.5,
    True,
    None,
    "0.5",
    float("nan"),
    float("inf"),
    10**30,
    -(10**30),
]


def _each_field(record: dict, values: list) -> list[dict]:
    return [{name: value} for name in record for value in values]


def test_the_body_agent_check_judges_every_record_as_python_does(
    generated_scenario, monkeypatch
) -> None:
    from sheltersim.domain.body_state import validate_body_state

    world = _world_with_extensions(generated_scenario)
    agents = world.model_state["body_state_v1"]["agents"]
    agent_id = next(iter(agents))
    _same_outcomes(
        world,
        validate_body_state,
        "body_agents_valid",
        monkeypatch,
        agents[agent_id],
        [{"sleep_debt_minutes": 1440}, {"sleep_debt_minutes": 1440.5}]
        + _each_field(agents[agent_id], _NUMBERS),
        lambda record: agents.__setitem__(agent_id, record),
    )


def test_the_psychology_check_judges_every_record_as_python_does(
    generated_scenario, monkeypatch
) -> None:
    from sheltersim.domain.psychological_state import validate_psychological_state

    world = _world_with_extensions(generated_scenario)
    state = world.model_state["psychological_distress_v1"]
    agent_id = next(iter(state["agents"]))
    record = {
        **state["agents"][agent_id],
        "daily_acute_sum": 1.5,
        "daily_observed_minutes": 3,
        "daily_high_distress_minutes": 2,
        "completed_days": 1,
    }
    _same_outcomes(
        state,
        validate_psychological_state,
        "psychological_agents_valid",
        monkeypatch,
        record,
        [
            {"daily_observed_minutes": 1439},
            {"daily_observed_minutes": 1440},
            {"daily_high_distress_minutes": 3},
            {"daily_high_distress_minutes": 4},
            {"daily_acute_sum": 3},
            {"daily_acute_sum": 3.0000000001},
            {"daily_acute_sum": 3.1},
        ]
        + _each_field(record, _NUMBERS),
        lambda value: state["agents"].__setitem__(agent_id, value),
    )
    for key in ("", 7):
        state["agents"] = {key: record}
        rust = _outcome(validate_psychological_state, state)
        with monkeypatch.context() as patch:
            patch.setattr(native, "psychological_agents_valid", lambda *a: False)
            assert rust == _outcome(validate_psychological_state, state)


def test_the_agent_dynamics_check_judges_every_record_as_python_does(
    generated_scenario, monkeypatch
) -> None:
    from sheltersim.domain.agent_dynamics import validate_agent_dynamics_state

    world = _world_with_extensions(generated_scenario)
    agents = world.model_state["agent_dynamics_v1"]["agents"]
    agent_id = next(iter(agents))
    facility = next(iter(world.facilities))
    _same_outcomes(
        world,
        validate_agent_dynamics_state,
        "agent_dynamics_valid",
        monkeypatch,
        agents[agent_id],
        [
            {"known_facility_ids": [facility]},
            {"known_facility_ids": [facility, facility]},
            {"known_facility_ids": ["nowhere"]},
            {"known_facility_ids": (facility,)},
            {"known_facility_ids": "text"},
        ]
        + [{"last_decision_minute": value} for value in (None, 0, 3, -1, True, 1.0, 10**30)]
        + [{"decision_due_minute": value} for value in (0, 3, -1, True, 1.0, 10**30)],
        lambda record: agents.__setitem__(agent_id, record),
    )


def test_the_position_check_judges_every_record_as_python_does(
    generated_scenario, monkeypatch
) -> None:
    from sheltersim.domain.movement import validate_movement_runtime_state

    world = _world_with_extensions(generated_scenario)
    positions = world.model_state["movement_v1"]["positions"]
    agent_id = next(iter(positions))
    edge = next(iter(world.zone_edges))
    _same_outcomes(
        world,
        validate_movement_runtime_state,
        "movement_positions_valid",
        monkeypatch,
        positions[agent_id],
        [
            {"zone_id": "nowhere"},
            {"zone_id": 7},
            {"edge_id": edge},
            {"edge_id": "nowhere"},
            {"point": None},
            {"point": {"x": 1.0, "y": 2}},
            {"point": {"x": float("inf"), "y": 2}},
            {"point": {"x": 1.0, "y": float("nan")}},
            {"point": {"x": True, "y": 2}},
            {"point": {"x": 1.0}},
            {"point": {"x": 1.0, "y": 2, "z": 3}},
            {"point": [1, 2]},
            {"position_kind": "physical"},
            {"position_kind": "display_only"},
            {"position_kind": "unavailable"},
            {"position_kind": "elsewhere"},
            {"position_kind": 7},
            {"progress": 0},
            {"progress": 1},
            {"progress": 1.0},
            {"progress": 1.5},
            {"progress": float("nan")},
            {"progress": True},
            {"progress": "0.5"},
        ],
        lambda record: positions.__setitem__(agent_id, record),
    )


def test_the_information_checks_judge_every_record_as_python_does(
    generated_scenario, monkeypatch
) -> None:
    from sheltersim.domain.information import validate_information_runtime_state

    world = _world_with_extensions(generated_scenario)
    state = world.model_state["information_spread_v1"]
    first, second = sorted(world.agents)[:2]
    key = f'["{first}","{second}"]'

    def validate(_world) -> None:
        validate_information_runtime_state(
            state,
            agent_ids=set(world.agents),
            zone_ids=set(world.zones),
            duration_minutes=world.duration_minutes,
        )

    record = {"last_seen_minute": 3, "consecutive_minutes": 2}
    _same_outcomes(
        world,
        validate,
        "information_contacts_valid",
        monkeypatch,
        record,
        [{"consecutive_minutes": value} for value in (0, 1, 4, 5, True, 1.0, 10**30)]
        + [
            {"last_seen_minute": value}
            for value in (-1, 0, world.duration_minutes - 1, world.duration_minutes, 10**30, True)
        ],
        lambda value: state["contacts"].__setitem__(key, value),
    )
    for bad_key in ("[", f'["{first}","{first}"]', f'["{first}", "{second}"]', '["a","b"]', 7):
        state["contacts"] = {bad_key: record}
        rust = _outcome(validate, world)
        with monkeypatch.context() as patch:
            patch.setattr(native, "information_contacts_valid", lambda *a: False)
            assert rust == _outcome(validate, world), bad_key
    state["contacts"] = {}
    agent_id = next(iter(state["query_pressure"]))
    for value in _NUMBERS:
        state["query_pressure"][agent_id] = value
        rust = _outcome(validate, world)
        with monkeypatch.context() as patch:
            patch.setattr(native, "unit_values_valid", lambda *a: False)
            assert rust == _outcome(validate, world), value


def test_the_agent_check_judges_every_agent_as_python_does(generated_scenario, monkeypatch) -> None:
    from sheltersim.domain.models import NeedState

    world = _world_with_extensions(generated_scenario)
    assert world.schema_version == 6
    agent = next(iter(world.agents.values()))

    def outcome() -> str:
        return _outcome(lambda w: w.validate(validate_event_history=False), world)

    def compare(change) -> None:
        rust = outcome()
        with monkeypatch.context() as patch:
            patch.setattr(native, "world_agents_valid", lambda *a: False)
            assert rust == outcome(), change

    fields = ("stress", "trust", "next_decision_at", "support_cooldown_until")
    for name in fields:
        original = getattr(agent, name)
        for value in _NUMBERS:
            setattr(agent, name, value)
            compare((name, value))
        setattr(agent, name, original)
    needs = agent.needs
    for name in ("toilet_urgency", "hunger", "fatigue", "hydration_need"):
        original = getattr(needs, name)
        for value in _NUMBERS:
            setattr(needs, name, value)
            compare((name, value))
        setattr(needs, name, original)
    agent.needs = {"hunger": 0.5}
    compare("needs of the wrong type")
    agent.needs = NeedState()
    zone = agent.zone_id
    agent.zone_id = "nowhere"
    compare("zone")
    agent.zone_id = zone
    assert outcome() == "valid"


def test_the_end_of_tick_check_agrees_with_the_plain_check_at_every_tick(
    generated_scenario, tmp_path, monkeypatch
) -> None:
    """Both pass at every tick of a Run, and the world's hash does not depend on the check."""
    from sheltersim.core.world import WorldState

    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 360
    scenario = ScenarioSpec.model_validate(document)
    original = WorldState.validate_after_tick
    checked = []

    def both(self) -> None:
        original(self)
        self.validate(validate_event_history=False)
        checked.append(self.minute_index)

    monkeypatch.setattr(WorldState, "validate_after_tick", both)
    with_memo = run_scenario(scenario, tmp_path / "memo", trace_level=TraceLevel.OFF)
    assert checked == list(range(1, 361))

    monkeypatch.setattr(
        WorldState, "validate_after_tick", lambda self: self.validate(validate_event_history=False)
    )
    plain = run_scenario(scenario, tmp_path / "plain", trace_level=TraceLevel.OFF)
    assert with_memo.state_digest() == plain.state_digest()


def test_the_rust_checks_accept_the_state_of_a_real_run(generated_scenario, tmp_path) -> None:
    """Otherwise every tick would fall back to the Python rules, and only be slower."""
    from sheltersim.core.tick_validation import PairKeys
    from sheltersim.domain.body_state import _pair_key_is_good
    from sheltersim.domain.information import _contact_key_is_good
    from sheltersim.domain.models import Activity, InitialNeedProfile, NeedState
    from sheltersim.domain.social_network import _key_is_good

    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 360
    world = run_scenario(ScenarioSpec.model_validate(document), tmp_path)
    state = world.model_state
    agents = world.agents
    agent_ids = set(agents)
    body = state["body_state_v1"]
    assert body["relationships"]
    assert native.world_agents_valid(agents, world.zones, Activity, InitialNeedProfile, NeedState)
    assert native.social_relationships_valid(
        state["social_network_v1"]["relationships"], set(), _key_is_good
    )
    assert native.body_relationships_valid(
        body["relationships"],
        PairKeys().good_keys(agent_ids),
        lambda key: _pair_key_is_good(key, agents=agents),
    )
    assert native.body_agents_valid(body["agents"])
    assert native.psychological_agents_valid(state["psychological_distress_v1"]["agents"])
    assert native.agent_dynamics_valid(state["agent_dynamics_v1"]["agents"], world.facilities)
    assert native.movement_positions_valid(
        state["movement_v1"]["positions"], world.zones, world.zone_edges
    )
    information = state["information_spread_v1"]
    assert native.information_contacts_valid(
        information["contacts"],
        PairKeys().good_keys(agent_ids),
        lambda key: _contact_key_is_good(key, agent_ids=agent_ids),
        world.duration_minutes,
    )
    assert native.unit_values_valid(information["query_pressure"])


def _same_bits_deep(x: object, y: object) -> bool:
    if isinstance(x, dict) and isinstance(y, dict):
        return list(x) == list(y) and all(_same_bits_deep(x[key], y[key]) for key in x)
    if isinstance(x, list) and isinstance(y, list):
        return len(x) == len(y) and all(_same_bits_deep(a, b) for a, b in zip(x, y, strict=True))
    return _same_float_bits(x, y)


def test_the_social_kernel_gives_the_python_result(
    generated_scenario, tmp_path, monkeypatch
) -> None:
    """Deltas, stored contacts and cooldowns (in the same order), traces and totals."""
    from sheltersim.policies.social import ExpressivenessChannelOpennessPolicy

    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 360
    scenario = ScenarioSpec.model_validate(document)
    compared = []
    contacts = []
    kernel = ExpressivenessChannelOpennessPolicy.native_update

    def checked(self, view, base_deltas, rng, *, zone_neighbors, runtime_state=None):
        rust = kernel(
            self,
            view,
            base_deltas,
            rng,
            zone_neighbors=zone_neighbors,
            runtime_state=runtime_state,
        )
        python = self.update(
            view,
            base_deltas,
            rng,
            zone_neighbors=zone_neighbors,
            runtime_state=runtime_state,
        )
        assert rust is not None
        _same_social_result(rust, python)
        assert rust.policy_version == python.policy_version
        compared.append(view.minute_index)
        contacts.append(len(rust.contacts))
        return rust

    monkeypatch.setattr(ExpressivenessChannelOpennessPolicy, "native_update", checked)
    run_scenario(scenario, tmp_path, trace_level=TraceLevel.OFF)
    assert compared == list(range(360))
    assert sum(contacts) > 0


@pytest.mark.parametrize(
    "value",
    [
        [],
        [-0.0],
        [0.1] * 10,
        [1e100, 1.0, -1e100, 1e-100],
        [1.0, 1e-16, 1e-16],
        [1.0, 1.1102230246251565e-16, 1.3877787807814457e-17],
        [-1.0, -1.1102230246251565e-16, -1.3877787807814457e-17],
        [1e308, 1e308, -1e308],
    ],
)
def test_the_kernel_sums_floats_as_math_fsum_does(value) -> None:
    import math

    import sheltersim_native

    try:
        python = math.fsum(value)
    except OverflowError:
        with pytest.raises(sheltersim_native.Unsupported):
            sheltersim_native.fsum(value)
        return
    assert _same_float_bits(sheltersim_native.fsum(value), python)


def test_the_kernel_sum_matches_math_fsum_on_random_floats() -> None:
    import math
    import random

    import sheltersim_native

    draw = random.Random(2615)
    for _ in range(20_000):
        count = draw.randrange(0, 9)
        values = [
            draw.choice((-1.0, 1.0)) * draw.random() * 2.0 ** draw.randrange(-60, 3)
            for _ in range(count)
        ]
        assert _same_float_bits(sheltersim_native.fsum(values), math.fsum(values)), values


def test_the_social_kernel_leaves_unusual_inputs_to_python(generated_scenario) -> None:
    from sheltersim.core.rng import CounterRNG
    from sheltersim.core.world import WorldState
    from sheltersim.policies.behavior import WorldView
    from sheltersim.policies.psychology import AgentStateDelta
    from sheltersim.policies.social import (
        ExpressivenessChannelOpennessPolicy,
        SocialPropagationParameters,
    )

    world = WorldState.from_scenario(generated_scenario)
    view = WorldView.from_world(world, include_state_hash=False)
    deltas = tuple(
        AgentStateDelta(
            agent_id=agent.id,
            minute_index=view.minute_index,
            toilet_urgency=0.0,
            hunger=0.0,
            fatigue=0.0,
            stress=0.5,
            trust=0.5,
        )
        for agent in view.agents.values()
    )
    policy = ExpressivenessChannelOpennessPolicy(SocialPropagationParameters(enabled=True))
    neighbors = {zone: () for zone in world.zones}
    rng = CounterRNG(1)

    def native_result(**changes):
        arguments = {
            "zone_neighbors": neighbors,
            "runtime_state": None,
            **changes,
        }
        return policy.native_update(view, deltas, rng, **arguments)

    assert native_result() is not None
    good = {"schema_version": 1, "contacts": {}, "agent_cooldown_until": {}}
    assert native_result(runtime_state=good) is not None
    for broken in (
        {**good, "schema_version": 9},
        {**good, "contacts": []},
        {**good, "contacts": {"x": {"last_seen_minute": True, "consecutive_minutes": 1}}},
        {**good, "contacts": {"x": {"last_seen_minute": -1, "consecutive_minutes": 1}}},
        {**good, "contacts": {"x": {"last_seen_minute": 1}}},
        {**good, "agent_cooldown_until": {"x": -1}},
        {**good, "agent_cooldown_until": {"x": 1.5}},
    ):
        assert native_result(runtime_state=broken) is None, broken
    assert policy.native_update(view, deltas[:-1], rng, zone_neighbors=neighbors) is None
    with_integer = ExpressivenessChannelOpennessPolicy(
        SocialPropagationParameters(enabled=True, stress_spread_per_minute=0)
    )
    assert with_integer.native_update(view, deltas, rng, zone_neighbors=neighbors) is None


def test_the_information_kernel_gives_the_python_state(
    generated_scenario, tmp_path, monkeypatch
) -> None:
    """The stored state, in the same order, and no proposals, at every tick."""
    from sheltersim.policies.behavior import WorldView
    from sheltersim.policies.information import InformationSpreadPolicy

    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 360
    scenario = ScenarioSpec.model_validate(document)
    compared = []
    kernel = InformationSpreadPolicy.native_update
    original_from_world = WorldView.from_world
    worlds = []

    def remember_world(world, **arguments):
        worlds.append(world)
        return original_from_world(world, **arguments)

    def checked(self, people, minute, runtime_state, rng, *, zone_neighbors):
        rust = kernel(self, people, minute, runtime_state, rng, zone_neighbors=zone_neighbors)
        python = self.update(
            original_from_world(worlds[-1], include_state_hash=False),
            runtime_state,
            rng,
            zone_neighbors=zone_neighbors,
        )
        assert rust is not None
        assert _same_bits_deep(rust.runtime_state, python.runtime_state)
        assert rust.proposals == python.proposals == ()
        assert rust.policy_version == python.policy_version
        compared.append(minute)
        return rust

    monkeypatch.setattr(WorldView, "from_world", remember_world)
    monkeypatch.setattr(InformationSpreadPolicy, "native_update", checked)
    run_scenario(scenario, tmp_path, trace_level=TraceLevel.OFF)
    assert compared == list(range(360))


def test_the_information_kernel_leaves_any_message_to_python(generated_scenario) -> None:
    from sheltersim.core.rng import CounterRNG
    from sheltersim.core.world import WorldState
    from sheltersim.domain.information import create_information_runtime_state
    from sheltersim.domain.models import InformationSpreadConfig
    from sheltersim.policies.information import InformationSpreadPolicy

    world = WorldState.from_scenario(generated_scenario)
    policy = InformationSpreadPolicy(InformationSpreadConfig(enabled=True))
    rng = CounterRNG(1)
    state = create_information_runtime_state(set(world.agents))
    neighbors = {zone: () for zone in world.zones}

    def result(changed):
        return policy.native_update(world.agents, 0, changed, rng, zone_neighbors=neighbors)

    assert result(state) is not None
    assert result({**state, "messages": {"m": {}}}) is None
    first = sorted(world.agents)[0]
    assert result({**state, "beliefs": {**state["beliefs"], first: {"m": {}}}}) is None
    assert result({**state, "support_requests": [{}]}) is None
    assert result({**state, "schema_version": 1}) is None
    assert result({**state, "contacts": []}) is None
    assert result({**state, "query_pressure": {}}) is None


def _same_social_result(rust, python) -> None:
    assert _same_bits_deep(rust.runtime_state, python.runtime_state)
    assert len(rust.deltas) == len(python.deltas)
    for a, b in zip(rust.deltas, python.deltas, strict=True):
        for name in a.__dataclass_fields__:
            assert _same_float_bits(getattr(a, name), getattr(b, name)), (a.agent_id, name)
    assert len(rust.contacts) == len(python.contacts)
    for a, b in zip(rust.contacts, python.contacts, strict=True):
        for name in a.__dataclass_fields__:
            assert _same_float_bits(getattr(a, name), getattr(b, name)), (a, name)
    assert rust.contributing_contact_count == python.contributing_contact_count
    assert _same_float_bits(rust.total_stress_added, python.total_stress_added)


def test_the_contact_kernels_match_python_on_random_inputs(generated_scenario) -> None:
    """IDs that JSON escapes, a zone next to itself, empty channels, zero sensitivity,
    exact caps and every cooldown and contact history a Run may not reach by itself."""
    import random
    from dataclasses import replace

    from sheltersim.core.rng import CounterRNG
    from sheltersim.core.world import WorldState
    from sheltersim.domain.information import create_information_runtime_state
    from sheltersim.domain.models import InformationSpreadConfig
    from sheltersim.policies.behavior import WorldView
    from sheltersim.policies.information import InformationSpreadPolicy
    from sheltersim.policies.psychology import AgentStateDelta
    from sheltersim.policies.social import (
        ExpressivenessChannelOpennessPolicy,
        SocialPropagationParameters,
    )

    draw = random.Random(2615)
    template = next(
        iter(
            WorldView.from_world(
                WorldState.from_scenario(generated_scenario), include_state_hash=False
            ).agents.values()
        )
    )
    names = ["A1", "A2", 'A"3', "A\\4", "é5", "ß6", "A\n7", "B8", "B9", "C10", "C11", "日12"]
    zones = ["z1", "z2", "zé", 'z"4']
    stresses = [0.0, 1.0, 0.5, 0.25, 1e-300, 1.0 - 2**-53]

    def unit() -> float:
        return draw.choice(stresses) if draw.random() < 0.3 else draw.random()

    raised = matched = 0
    for trial in range(400):
        minute = draw.randrange(0, 50)
        ids = draw.sample(names, draw.randrange(1, len(names) + 1))
        agents = {
            agent_id: replace(
                template,
                id=agent_id,
                zone_id=draw.choice(zones),
                group_id=draw.choice((None, "g1", "g2", 'g"3')),
                stress=unit(),
                stress_sensitivity=draw.choice((0.0, 1.0, draw.random())),
                trust=unit(),
            )
            for agent_id in ids
        }
        view = WorldView(minute_index=minute, agents=agents, facilities={}, state_hash=None)
        neighbors = {
            zone: tuple(draw.sample(zones, draw.randrange(0, len(zones) + 1)))
            for zone in zones
            if draw.random() < 0.8
        }
        pairs = [(a, b) for a in names for b in names if a != b and draw.random() < 0.3]
        contacts = {
            json.dumps([a, b], ensure_ascii=False, separators=(",", ":")): {
                "last_seen_minute": draw.choice((minute - 1 if minute else 0, minute, 0)),
                "consecutive_minutes": draw.randrange(1, 12),
            }
            for a, b in pairs
        }
        deltas = tuple(
            AgentStateDelta(
                agent_id=agent_id,
                minute_index=minute,
                toilet_urgency=0.0,
                hunger=0.0,
                fatigue=0.0,
                stress=unit(),
                trust=0.5,
            )
            for agent_id in ids
        )
        rng = CounterRNG(draw.randrange(0, 2**70))
        parameters = SocialPropagationParameters(
            enabled=True,
            max_contacts_per_agent=draw.randrange(0, 5),
            minimum_contact_minutes=draw.randrange(1, 6),
            stress_spread_per_minute=draw.choice((0.0, 0.25, draw.random() / 4)),
            max_pair_stress_increase=draw.choice((0.0, 0.05, 1e-6, draw.random() / 4)),
            max_agent_stress_increase_per_minute=draw.choice((0.0, 0.05, draw.random() / 4)),
            cooldown_minutes=draw.randrange(1, 40),
            source_expressiveness_by_agent={
                name: draw.choice((0.0, 1.0, draw.random()))
                for name in names
                if draw.random() < 0.5
            },
            family_channel_strength=draw.choice((0.0, 1.0, draw.random())),
            same_zone_channel_strength=draw.choice((0.0, 1.0, draw.random())),
            nearby_zone_channel_strength=draw.choice((0.0, 1.0, draw.random())),
            source_id="ASM-BOSSE-AFFECT-CONTAGION-V1",
        )
        runtime_state = {
            "schema_version": 1,
            "contacts": contacts,
            "agent_cooldown_until": {
                name: draw.randrange(0, 60) for name in names if draw.random() < 0.4
            },
        }
        social = ExpressivenessChannelOpennessPolicy(parameters)
        arguments = {"zone_neighbors": neighbors, "runtime_state": runtime_state}
        rust = social.native_update(view, deltas, rng, **arguments)
        try:
            python = social.update(view, deltas, rng, **arguments)
        except ValueError:
            # Contacts of no weight stop the Python model; the kernel leaves them to it.
            assert rust is None, trial
            raised += 1
        else:
            assert rust is not None, trial
            _same_social_result(rust, python)
            matched += 1

        information = InformationSpreadPolicy(
            InformationSpreadConfig(
                enabled=True,
                max_contacts_per_agent=draw.randrange(0, 5),
                query_pressure_decay_per_minute=draw.choice((0.0, 0.25, draw.random() / 4)),
            )
        )
        state = create_information_runtime_state(set(ids))
        state["contacts"] = contacts
        state["query_pressure"] = {agent_id: unit() for agent_id in ids}
        state["counters"]["contact_attempts"] = draw.randrange(0, 100)
        rust_state = information.native_update(agents, minute, state, rng, zone_neighbors=neighbors)
        assert rust_state is not None, trial
        python_state = information.update(view, state, rng, zone_neighbors=neighbors)
        assert _same_bits_deep(rust_state.runtime_state, python_state.runtime_state), trial
    assert matched > 100 and raised > 0


def _event_arguments(**changes) -> list:
    fields = {
        "event_id": "event-1",
        "event_type": "tick",
        "source_id": None,
        "causation_id": "event-0",
        "due_minute": 3,
        "sequence": 4,
        "priority": -1,
        "payload": {},
        "provenance": {},
    }
    return list((fields | changes).values())


def test_the_event_kernel_freezes_values_as_python_does() -> None:
    from types import MappingProxyType

    from sheltersim.events.models import EventKind, ScheduledEvent, _freeze_json

    payload = {
        "a": [1, 2.5, "x", None, True, {"b": ()}],
        "c": {"d": [[]], "e": -0.0, "é": 10**30},
    }
    frozen, provenance = native.scheduled_event_checked(
        *_event_arguments(payload=payload, provenance={"p": [1]})
    )
    assert frozen == _freeze_json(payload)
    assert type(frozen) is MappingProxyType and type(frozen["a"]) is tuple
    assert type(frozen["a"][5]) is MappingProxyType
    assert type(frozen["c"]["d"][0]) is tuple
    assert provenance == _freeze_json({"p": [1]})
    assert list(frozen) == list(payload)
    event = ScheduledEvent("event-1", EventKind.ENDOGENOUS, 3, -1, 4, "tick", payload)
    assert event.to_dict()["payload"] == {
        "a": [1, 2.5, "x", None, True, {"b": []}],
        "c": {"d": [[]], "e": -0.0, "é": 10**30},
    }
    payload["c"]["d"].append(1)
    assert event.payload["c"]["d"] == ((),)


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": "-starts-badly"},
        {"event_id": "x" * 65},
        {"event_id": None},
        {"event_id": "with\nnewline"},
        {"event_id": "é"},
        {"event_id": "\ud800"},
        {"event_type": ""},
        {"source_id": "bad id"},
        {"causation_id": 1},
        {"due_minute": -1},
        {"due_minute": True},
        {"due_minute": 1.0},
        {"sequence": -1},
        {"priority": False},
        {"payload": []},
        {"provenance": None},
        {"payload": {"a": float("nan")}},
        {"payload": {"a": [float("inf")]}},
        {"payload": {1: 2}},
        {"payload": {"a": object()}},
        {"payload": {"a": {1, 2}}},
        {"provenance": {"a": b"bytes"}},
    ],
)
def test_the_event_kernel_leaves_what_python_would_decide(changes) -> None:
    """Each of these either raises in the Python checks or needs them to decide."""
    from sheltersim.events.models import EventKind, ScheduledEvent

    assert native.scheduled_event_checked(*_event_arguments(**changes)) is None
    event_id, event_type, source_id, causation_id, due, sequence, priority, payload, provenance = (
        _event_arguments(**changes)
    )
    with pytest.raises((ValueError, TypeError)):
        ScheduledEvent(
            event_id,
            EventKind.ENDOGENOUS,
            due,
            priority,
            sequence,
            event_type,
            payload,
            source_id,
            causation_id,
            provenance,
        )


def test_the_event_kernel_leaves_frozen_values_and_subclasses_to_python() -> None:
    from types import MappingProxyType

    from sheltersim.events.models import EventKind, ScheduledEvent

    class Text(str):
        pass

    class Number(float):
        pass

    frozen = MappingProxyType({"a": (1, 2)})
    for payload in ({"a": frozen}, {"a": Text("x")}, {"a": Number(1.0)}, {Text("k"): 1}):
        assert native.scheduled_event_checked(*_event_arguments(payload=payload)) is None
    event = ScheduledEvent(
        "event-1", EventKind.ENDOGENOUS, 3, 0, 4, "tick", {"a": frozen, "b": Text("x")}
    )
    assert event.to_dict()["payload"] == {"a": {"a": [1, 2]}, "b": "x"}


def test_the_view_hash_kernel_gives_the_python_hash_at_every_tick(
    generated_scenario, tmp_path, monkeypatch
) -> None:
    from sheltersim import policies

    behavior = __import__("sheltersim.policies.behavior", fromlist=["_view_state_hash"])
    assert policies is not None
    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 240
    kernel = native.view_state_hash
    original = behavior._view_state_hash
    compared = []

    def checked(*arguments):
        rust = original(*arguments)
        assert kernel is native.view_state_hash
        monkeypatch.setattr(native, "view_state_hash", lambda *_: None)
        try:
            python = original(*arguments)
        finally:
            monkeypatch.setattr(native, "view_state_hash", kernel)
        assert rust == python
        compared.append(rust)
        return rust

    monkeypatch.setattr(behavior, "_view_state_hash", checked)
    run_scenario(ScenarioSpec.model_validate(document), tmp_path, trace_level=TraceLevel.OFF)
    assert len(compared) == 240


@pytest.mark.parametrize("information", [False, True])
@pytest.mark.parametrize("schema_version", [None, 6])
@pytest.mark.parametrize("life_context", [False, True])
def test_the_view_hash_kernel_covers_each_form_of_the_document(
    generated_scenario, monkeypatch, information, schema_version, life_context
) -> None:
    """The document's optional parts, with unusual but valid values in the people's fields."""
    from dataclasses import replace

    from sheltersim.core.world import WorldState
    from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
    from sheltersim.policies import behavior

    world = WorldState.from_scenario(generated_scenario)
    if schema_version is not None:
        world.schema_version = schema_version
        world.model_configs.setdefault("resource_service", {"x": [1.5, None]})
    if not information:
        world.model_state.pop(INFORMATION_SPREAD_STATE_KEY, None)
        state = None
    else:
        state = world.model_state.setdefault(INFORMATION_SPREAD_STATE_KEY, {"query_pressure": {}})
        state["query_pressure"] = {agent_id: 0.25 for agent_id in world.agents}
        world.model_configs.setdefault("information_spread", {"y": True})
    world.model_state.pop("body_state_v1", None)
    view = behavior.WorldView.from_world(world, include_state_hash=False)
    first = next(iter(view.agents))
    agents = {
        **view.agents,
        first: replace(
            view.agents[first],
            target_id='quote"and\\slash\n',
            stress=1e-05,
            trust=-0.0,
            support_wait_minutes=10**20,
            roles=("role", "役割"),
            usual_wake_minute=None,
        ),
    }
    arguments = (world, agents, view.facilities, state, life_context)
    rust = behavior._view_state_hash(*arguments)
    monkeypatch.setattr(native, "view_state_hash", lambda *_: None)
    assert rust == behavior._view_state_hash(*arguments)


def test_the_view_hash_kernel_leaves_values_json_rejects_to_python(generated_scenario) -> None:
    from dataclasses import replace

    from sheltersim.core.world import WorldState
    from sheltersim.policies import behavior

    world = WorldState.from_scenario(generated_scenario)
    view = behavior.WorldView.from_world(world, include_state_hash=False)
    first = next(iter(view.agents))
    agents = {**view.agents, first: replace(view.agents[first], stress=float("nan"))}
    with pytest.raises(ValueError):
        behavior._view_state_hash(world, agents, view.facilities, None, False)


def test_the_encoder_writes_floats_as_repr_does() -> None:
    import math
    import random

    rng = random.Random(20261031)
    edges = [
        1e-4,
        math.nextafter(1e-4, 0.0),
        math.nextafter(1e-4, 1.0),
        1e16,
        math.nextafter(1e16, 0.0),
        9007199254740993.0,
        1.0,
        0.1,
        5e-324,
        1.7976931348623157e308,
        0.0,
        -0.0,
        123456.0,
        1e15,
        1e22,
    ]
    values = [*edges, *(-value for value in edges)]
    for _ in range(50_000):
        values.append(struct.unpack("<d", rng.getrandbits(64).to_bytes(8, "little"))[0])
        values.append(rng.random())
        values.append(rng.random() * 10.0 ** rng.randrange(-8, 20))
        values.append(round(rng.random() * 1440.0, rng.randrange(6)))
    import sheltersim_native

    for value in values:
        if math.isfinite(value):
            assert sheltersim_native.float_json(value) == repr(value)


def test_the_event_kernel_leaves_ints_of_any_size_to_python() -> None:
    from sheltersim.events.models import EventKind, ScheduledEvent

    assert native.scheduled_event_checked(*_event_arguments(sequence=2**70)) is None
    assert ScheduledEvent("e-1", EventKind.ENDOGENOUS, 1, 0, 2**70, "tick").sequence == 2**70
