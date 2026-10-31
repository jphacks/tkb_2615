"""The Rust kernels (feature/rust-port, native/) give the same values as the Python
code they replace, bit for bit, at every tick of a generated Scenario.

Skipped when the sheltersim_native extension is not installed; CI on the Rust port
branch builds and installs it.
"""

from __future__ import annotations

import copy
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from support.paths import BENCHMARKS

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
    from sheltersim.experiments.phases.update import UpdatePhase
    from sheltersim.experiments.runner import OfflineSimulationController

    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 360
    scenario = ScenarioSpec.model_validate(document)
    compared = []
    kernel = OfflineSimulationController._psychology_update
    python = UpdatePhase._psychology_update

    def checked(self, view, minute):
        result = kernel(self, view, minute)
        rust = result.deltas
        expected = python(self, view, minute).deltas
        assert len(rust) == len(expected)
        for a, b in zip(rust, expected, strict=True):
            for name in a.__dataclass_fields__:
                assert _same_float_bits(getattr(a, name), getattr(b, name)), (
                    minute,
                    a.agent_id,
                    name,
                )
        compared.append(minute)
        return result

    monkeypatch.setattr(OfflineSimulationController, "_psychology_update", checked)
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
    assert native.agent_dynamics_valid(state["agent_dynamics_v1"]["agents"], world.facilities)
    assert native.movement_positions_valid(
        state["movement_v1"]["positions"], world.zones, world.zone_edges
    )


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


@pytest.mark.python_rules
def test_the_view_hash_kernel_gives_the_python_hash_at_every_tick(
    generated_scenario, tmp_path, monkeypatch
) -> None:
    from sheltersim import policies

    world_view = __import__("sheltersim.policies.world_view", fromlist=["_view_state_hash"])
    assert policies is not None
    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 240
    kernel = native.view_state_hash
    original = world_view._view_state_hash
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

    monkeypatch.setattr(world_view, "_view_state_hash", checked)
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


_INDEX_ROWS = [
    {"minute": 1},
    {
        "minute": 1,
        "event_id": "e-1",
        "causation_id": "e-0",
        "agent_id": "a-1",
        "actor_ids": ["a", 1],
    },
    # An identity key is not read below; an event reference key is, if it holds no str.
    {"minute": 1, "agent_id": {"zone_id": "z", "event_id": "e-2"}, "list_id": [{"staff_id": "s"}]},
    {"minute": 1, "event_id": {"event_id": "e-3", "zone_id": "z"}, "causation_id": ["e-4"]},
    {"minute": 1, "event_ids": ["e-5", 1, "e-5"], "source_event_ids": [], "x_id": None},
    {"minute": 1, "source_event_ids": "e-6", "modifier_event_ids": ["e-7"], "other": 1.5},
    {
        "minute": 1,
        "causation_event_ids": ["e-8", ["e-9"]],
        "environmental_stressor_event_ids": ["e"],
    },
    {"minute": 1, "rows": [{"agent_id": "a", "more": [[{"event_id": "e-10", "b_ids": ["b"]}]]}]},
    {"minute": 1, "文": '避難所\n"', "big": 2**70, "flag": True, "none": None, "float": 1e22},
    {"minute": 1, "note": "call 090-1234-5678", "contact": "a@example.com"},
    {"minute": 1, "api_key": "x"},
]


@pytest.mark.parametrize("row", _INDEX_ROWS)
def test_index_row_reads_a_row_as_the_python_functions_do(row: dict) -> None:
    import hashlib

    from sheltersim.storage.history_index import _TOKEN_FIELDS, _event_references, _identities
    from sheltersim.storage.projects import _private_field, canonical

    read = native.index_row(row)
    assert read is not None
    encoded, payload_hash, identities, references, keys, strings, tokens = read
    keys = keys.split("\0")
    assert encoded == canonical(row)
    assert payload_hash == hashlib.sha256(encoded.encode()).hexdigest()
    assert set(identities) == _identities(row)
    assert set(references) == _event_references(row)
    assert (set(keys), set(strings)) == tuple(
        map(set, native.sensitive_candidates([encoded.encode()]))
    )
    if "api_key" in row:
        assert any(map(_private_field, keys))
    assert tokens == " ".join(str(row.get(name, "")) for name in _TOKEN_FIELDS)


@pytest.mark.parametrize(
    "row",
    [
        {"minute": 1, "values": (1, 2)},
        {"minute": 1, "agent_id": ("a", "b")},
        {"minute": 1, 2: "a"},
        {"minute": 1, "inner": {2: "a"}},
        {"minute": float("nan")},
        {"minute": 1, "value": object()},
        {"minute": 1, "ids": [{1: 2}]},
    ],
)
def test_index_row_leaves_any_other_row_to_python(row: dict) -> None:
    assert native.index_row(row) is None


def test_a_row_stored_through_the_kernel_is_the_row_stored_without_it(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from sheltersim.storage.history_index import HistoryIndex, _RecordBatch
    from sheltersim.storage.projects import ProjectStore

    def stored(name: str) -> list:
        root = tmp_path / name
        (root / "p" / "runs" / "r").mkdir(parents=True)
        index = HistoryIndex(ProjectStore(root), "p", "r")
        with index.connect(write=True) as db, _RecordBatch(db, "main") as records:
            for row in _INDEX_ROWS:
                # A container in a record's columns is an error with or without the kernel.
                if "api_key" not in row and not isinstance(row.get("event_id"), dict):
                    records.put("events", row)
            records.put("events", {"minute": 1, "trigger": {"b": "two words", "a": "one"}})
            records.put("events", {"minute": 1, "trigger": ("c", "d")})
        with sqlite3.connect(index.path) as db:
            return [
                sorted(db.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
                for table in ("records", "entities", "tokens")
            ]

    with_kernel = stored("with")
    monkeypatch.setattr(native, "index_row", lambda _row: None)
    assert stored("without") == with_kernel


def _record(payload: dict, *, kind: str = "endogenous", status: str = "applied", **fields: object):
    from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent

    event = ScheduledEvent(
        "event-1",
        EventKind(kind),
        3,
        -1,
        7,
        "tick",
        payload=payload,
        source_id=fields.pop("source_id", None),
        provenance={"origin": "test", "values": [1, 2.5]},
    )
    return EventLogRecord(event, 4, EventStatus(status), fields.pop("detail", None))


def _same_records(records: list, raws: list[bytes]) -> bool:
    from sheltersim.events.models import EventLogRecord, ScheduledEvent

    return native.same_saved_records(records, raws, EventLogRecord, ScheduledEvent)


_SAMPLE_PAYLOAD = {
    "int": 1,
    "big": 2**80,
    "float": 1.0,
    "tiny": 1e-300,
    "huge": 1e22,
    "minus_zero": -0.0,
    "flag": True,
    "none": None,
    "text": '\u6587"\\\n',
    "list": [1, [2.5, {"a": []}], {}],
    "object": {"b": {"c": [None]}},
}


@pytest.mark.parametrize(
    "fields",
    [{}, {"detail": "why", "source_id": "source-1"}],
)
def test_same_saved_records_accepts_a_record_and_its_saved_bytes(fields: dict) -> None:
    from sheltersim.events.queue import record_bytes, record_from_bytes

    record = _record(_SAMPLE_PAYLOAD, **fields)
    raw = record_bytes(record)
    assert record_from_bytes(raw) == record
    assert _same_records([record], [raw])
    assert _same_records([], [])


@pytest.mark.parametrize(
    "payload",
    [
        {},
        _SAMPLE_PAYLOAD,
        {"z": 1, "a": {"y": [], "b": {}}, "\u6587": "\u00e9\U0001f600", "\ud7ff": -1.5e-7},
    ],
)
def test_record_json_is_the_canonical_json_of_to_dict(payload: dict) -> None:
    from sheltersim.events.models import EventLogRecord, ScheduledEvent

    for fields in ({}, {"detail": "\u6587", "source_id": "source-1"}):
        record = _record(payload, **fields)
        expected = native.canonical_json(record.to_dict())
        assert native.record_json(record, EventLogRecord, ScheduledEvent) == expected


def test_record_json_leaves_other_values_to_python() -> None:
    import dataclasses
    from types import MappingProxyType

    from sheltersim.events.models import EventLogRecord, ScheduledEvent

    class Text(str):
        pass

    types = (EventLogRecord, ScheduledEvent)
    assert native.record_json(object(), *types) is None
    record = _record({"text": "a"})
    plain = dataclasses.replace(record, event=_record({"text": "a"}).event)
    object.__setattr__(plain.event, "payload", {"text": "a"})
    assert native.record_json(plain, *types) is None
    subclass = dataclasses.replace(record, event=_record({"text": Text("a")}).event)
    assert native.record_json(subclass, *types) is None
    nonfinite = dataclasses.replace(record, event=_record({}).event)
    object.__setattr__(nonfinite.event, "payload", MappingProxyType({"x": float("nan")}))
    assert native.record_json(nonfinite, *types) is None


@pytest.mark.parametrize(
    "changed",
    [
        {"int": 2},
        {"int": 1.0},
        {"int": True},
        {"float": 1},
        {"flag": 1},
        {"none": 0},
        {"none": "null"},
        {"text": "other"},
        {"list": [1, [2.5, {"a": [0]}], {}]},
        {"list": [1, [2.5, {"a": []}]]},
        {"object": {"b": {"c": [None], "d": 1}}},
        {"object": {"b": {"d": [None]}}},
        {"object": {}},
        {"added": 1},
    ],
)
def test_same_saved_records_refuses_a_record_of_other_values(changed: dict) -> None:
    from sheltersim.events.queue import record_bytes

    raw = record_bytes(_record(_SAMPLE_PAYLOAD))
    assert not _same_records([_record({**_SAMPLE_PAYLOAD, **changed})], [raw])


@pytest.mark.parametrize(
    "other",
    [
        {"status": "failed"},
        {"kind": "exogenous"},
        {"detail": "why"},
        {"source_id": "source-2"},
    ],
)
def test_same_saved_records_refuses_other_fields(other: dict) -> None:
    from sheltersim.events.queue import record_bytes

    raw = record_bytes(_record({}))
    assert not _same_records([_record({}, **other)], [raw])


def test_same_saved_records_reads_every_field_of_the_bytes() -> None:
    from sheltersim.events.queue import record_bytes

    record = _record({})
    document = json.loads(record_bytes(record))
    for changed in (
        {**document, "extra": 1},
        {**document, "event": {**document["event"], "extra": 1}},
        {key: value for key, value in document.items() if key != "detail"},
        {**document, "realized_minute": 5},
        {**document, "event": {**document["event"], "sequence": 8}},
        {**document, "event": {**document["event"], "priority": 0}},
        {**document, "event": {**document["event"], "provenance": {}}},
    ):
        assert not _same_records([record], [json.dumps(changed).encode()])
    assert not _same_records([record], [b"not json"])
    assert not _same_records([record], [])
    assert not _same_records([object()], [record_bytes(record)])


def test_same_saved_records_leaves_other_mappings_and_subclasses_to_the_caller() -> None:
    import dataclasses

    from sheltersim.events.queue import record_bytes

    class Text(str):
        pass

    record = _record({"text": "a"})
    raw = record_bytes(record)
    plain = dataclasses.replace(record)
    object.__setattr__(plain.event, "payload", {"text": "a"})
    assert not _same_records([plain], [raw])
    object.__setattr__(plain.event, "payload", _record({"text": Text("a")}).event.payload)
    assert not _same_records([plain], [raw])


def _same_items(current: list, documents: list, start: int = 0) -> bool:
    return native.same_json_items(current, documents, start, len(current))


def test_same_json_items_accepts_only_exactly_the_same_values() -> None:
    entry = {"a": 1, "b": [1.5, None, True, "x", {"c": [2]}], "d": 10**30}
    document = json.loads(json.dumps(entry))
    assert _same_items([entry], [document])
    assert _same_items([entry], [{"dropped": 1}, document], 1)
    for other in (
        {**entry, "a": True},
        {**entry, "a": 1.0},
        {**entry, "a": 2},
        {**entry, "d": 10**30 + 1},
        {**entry, "b": tuple(entry["b"])},
        {**entry, "b": [1.5, None, True, "y", {"c": [2]}]},
        {**entry, "b": [1.5, None, 1, "x", {"c": [2]}]},
        {**entry, "b": [1.5, None, True, "x", {"c": [2], "e": 1}]},
        {**entry, "extra": 1},
        {key: value for key, value in entry.items() if key != "a"},
        {**{key: value for key, value in entry.items() if key != "a"}, "e": 1},
    ):
        assert not _same_items([other], [document]), other


def test_same_json_items_tells_float_zeros_and_leaves_other_types_out() -> None:
    assert not _same_items([{"a": -0.0}], [{"a": 0.0}])

    class Text(str):
        pass

    assert not _same_items([{"a": Text("x")}], [{"a": "x"}])
    assert not _same_items([{1: "x"}], [{"1": "x"}])
    deep: list = []
    for _ in range(40):
        deep = [deep]
    assert not _same_items([deep], [copy.deepcopy(deep)])


def test_same_json_items_rejects_counts_outside_either_list() -> None:
    assert not native.same_json_items([1], [1], 0, 2)
    assert not native.same_json_items([1, 2], [1], 0, 2)
    assert not native.same_json_items([1], [1], 1, 1)
    assert native.same_json_items([1, 2], [1], 0, 1)
    assert not native.same_json_items((1,), [1], 0, 1)


def test_a_rust_probability_has_the_bits_python_compared_with_its_draws(tmp_path: Path) -> None:
    """The worked example of docs/rust-port/lanes/p0-digest.md: a probability that flips no
    draw leaves the hashes alone, so its bits are compared at the comparison itself.

    The expm1 of the parity module stands in for the lane's own function.
    """
    output = tmp_path / "python"
    subprocess.run(
        [
            sys.executable,
            str(BENCHMARKS / "first_difference.py"),
            "--record",
            "model__information__evidence-aware-rumor-v1",
            "--output",
            str(output),
            "--draws",
        ],
        # The Python rule stays the reference after its Rust port.
        env={**os.environ, "SHELTERSIM_LLM_MODE": "offline", "SHELTERSIM_NATIVE": "0"},
        check=True,
        capture_output=True,
    )
    python_compared_with = set()
    with (output / "draws.jsonl").open(encoding="utf-8") as rows:
        for line in rows:
            row = json.loads(line)
            if row["k"][0] == "information-forgetting-v1":
                python_compared_with.update(use[1] for use in row["u"] if use[0] == "lt")
    # gate_scenarios.with_rumors; the lane's input comes from the same Scenario.
    rate = 0.002

    rust = -native.rules("parity").expm1([-rate])[0]

    assert python_compared_with == {rust.hex()}
    # What the gate's hashes cannot see (docs/rust-port.md): one percent is a different value.
    assert (rust * 1.01).hex() not in python_compared_with
