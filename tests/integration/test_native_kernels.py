"""The Rust kernels (feature/rust-port, native/) give the same values as the Python
code they replace, bit for bit, at every tick of a generated Scenario.

Skipped when the sheltersim_native extension is not installed; CI on the Rust port
branch builds and installs it.
"""

from __future__ import annotations

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
