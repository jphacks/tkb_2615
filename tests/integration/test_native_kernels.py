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
    same first error) from the Rust check as from the Python loop."""
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
            patch.setattr(social_network.native, "social_relationships_problem", lambda *a: None)
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
            patch.setattr(body_state.native, "body_relationships_problem", lambda *a: None)
            python = _outcome(validate_body_state, world)
        assert rust == python, case


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
