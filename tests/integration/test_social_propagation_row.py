"""social_propagation_v1 owned by Rust, and the social model's step in native/src/rules.

policies/social.py and policies/alternatives.py stay as the comparison base
(docs/rust-port/lanes/b-social-propagation.md). The bits of the whole rule are compared on the gate
by benchmarks/rule_shadow; here, on small worlds and on values no scenario reaches.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.engine import TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.experiments.phases.update import UpdatePhase
from sheltersim.experiments.runner import OfflineSimulationController, run_scenario
from sheltersim.policies.alternatives import DeGrootContactPolicy
from sheltersim.policies.behavior import WorldView
from sheltersim.policies.psychology import AgentStateDelta
from sheltersim.policies.social import (
    SOCIAL_RUNTIME_STATE_KEY,
    ExpressivenessChannelOpennessPolicy,
    SocialPropagationParameters,
    StressPropagationPolicy,
)

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

KEY = SOCIAL_RUNTIME_STATE_KEY
BOSSE = "ASM-BOSSE-AFFECT-CONTAGION-V1"
DEGROOT = "ASM-DEGROOT-CONTACT-V1"
POLICIES = {
    "sparse": (StressPropagationPolicy, "ASM-SOCIAL-PROPAGATION-V1"),
    "channel": (ExpressivenessChannelOpennessPolicy, BOSSE),
    "degroot": (DeGrootContactPolicy, DEGROOT),
}


def _rules() -> Any:
    return native.rules("social_propagation")


def _state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contacts": {
            '["a","b"]': {"last_seen_minute": 4, "consecutive_minutes": 2},
            '["b","a"]': {"last_seen_minute": 3, "consecutive_minutes": 1},
        },
        "agent_cooldown_until": {"b": 9, "a": 7},
    }


def _world(state: dict[str, Any] | None = None) -> WorldState:
    return WorldState("s", 1, 100, model_state={KEY: state if state is not None else _state()})


def _mirror(world: WorldState) -> Any:
    return world._core.mirror(KEY)


# ---- The row -----------------------------------------------------------------------------------


def test_a_row_writes_the_bytes_of_the_tree_with_its_keys_sorted() -> None:
    world = _world()
    assert _mirror(world).row_json() == native.canonical_json(_state())
    assert _mirror(world).verify()
    assert list(world.model_state[KEY]["agent_cooldown_until"]) == ["b", "a"]


@pytest.mark.parametrize(
    "write",
    [
        lambda state: state["contacts"]['["a","b"]'].__setitem__("last_seen_minute", 1.0),
        lambda state: state["contacts"]['["a","b"]'].__setitem__("consecutive_minutes", True),
        lambda state: state["contacts"]['["a","b"]'].__setitem__("other", 1),
        lambda state: state["contacts"]['["a","b"]'].pop("consecutive_minutes"),
        lambda state: state["contacts"].__setitem__("x", {"last_seen_minute": 1}),
        lambda state: state["agent_cooldown_until"].__setitem__("x", 1.5),
        lambda state: state.__setitem__("other", 1),
        lambda state: state.__setitem__("schema_version", "1"),
    ],
)
def test_a_value_the_row_cant_hold_taints_the_namespace(write: Any) -> None:
    world = _world()
    write(world.model_state[KEY])
    assert _mirror(world).tainted is not None
    assert not _mirror(world).has_row


def test_forwarded_writes_reach_the_row() -> None:
    world = _world()
    state = world.model_state[KEY]
    state["contacts"].pop('["a","b"]')
    state["agent_cooldown_until"].pop("a", None)
    state["contacts"]['["c","d"]'] = {"last_seen_minute": 5, "consecutive_minutes": 1}
    assert _mirror(world).verify()
    state["contacts"] = {k: v for k, v in state["contacts"].items() if "c" not in k}
    assert _mirror(world).verify()
    assert json.loads(_mirror(world).row_json())["contacts"].keys() == {'["b","a"]'}


# ---- The step on small worlds ------------------------------------------------------------------


def _small(
    generated_scenario: Any, *, policy: str = "channel", **parameters: Any
) -> tuple[Any, ...]:
    """A world with a row, a controller, and the arguments of the step at the next minute."""
    world = WorldState.from_scenario(generated_scenario)
    world.model_state[KEY] = {"schema_version": 1, "contacts": {}, "agent_cooldown_until": {}}
    for index, agent in enumerate(world.agents.values()):
        agent.stress = (index % 10) / 10.0
    view = WorldView.from_world(world, include_state_hash=False)
    kind, source = POLICIES[policy]
    settings = SocialPropagationParameters(
        enabled=True, stress_spread_per_minute=0.2, minimum_contact_minutes=1, source_id=source
    )
    controller = SimpleNamespace(
        stress_policy=kind(replace(settings, **parameters)),
        artifacts=SimpleNamespace(trace_level=TraceLevel.OFF),
        zone_neighbors={zone: () for zone in world.zones},
    )
    deltas = tuple(
        AgentStateDelta(
            agent_id=agent_id,
            minute_index=view.minute_index,
            toilet_urgency=0.0,
            hunger=0.0,
            fatigue=0.0,
            stress=0.5,
            trust=0.5,
        )
        for agent_id in sorted(view.agents)
    )
    return world, controller, view, deltas, CounterRNG(1)


def _python_result(world: Any, controller: Any, view: Any, deltas: Any, rng: Any) -> Any:
    state = json.loads(json.dumps(world.model_state[KEY]))
    return controller.stress_policy.update(
        view, deltas, rng, zone_neighbors=controller.zone_neighbors, runtime_state=state
    )


def _run_kernel(world: Any, controller: Any, view: Any, deltas: Any, rng: Any) -> Any:
    return _rules().update_social_propagation(
        controller, world, view, deltas, view.minute_index, rng
    )


@pytest.mark.parametrize("policy", POLICIES)
def test_the_step_writes_what_the_python_rule_writes(generated_scenario: Any, policy: str) -> None:
    arguments = _small(generated_scenario, policy=policy, cooldown_minutes=2)
    world = arguments[0]
    python = _python_result(*arguments)
    final = _run_kernel(*arguments)
    assert json.dumps(world.model_state[KEY]) == json.dumps(python.runtime_state)
    assert _mirror(world).row_json() == native.canonical_json(python.runtime_state)
    assert [asdict(delta) for delta in final] == [asdict(delta) for delta in python.deltas]
    assert any(delta.stress != 0.5 for delta in final)


def test_records_change_keys_and_later_writes_reach_their_new_place(
    generated_scenario: Any,
) -> None:
    world, controller, view, deltas, rng = _small(generated_scenario)
    for minute in range(3):
        moved = replace(view, minute_index=minute)
        shifted = tuple(replace(delta, minute_index=minute) for delta in deltas)
        _run_kernel(world, controller, moved, shifted, rng)
        assert _mirror(world).verify()
    state = world.model_state[KEY]
    assert _mirror(world).stats()["whole_writes"] == 1
    # The root, two maps and the records, so nothing is left in the table.
    assert _mirror(world).stats()["live_nodes"] == 3 + len(state["contacts"])
    key = next(iter(state["contacts"]))
    state["contacts"][key]["consecutive_minutes"] = 99
    assert _mirror(world).verify()
    assert json.loads(_mirror(world).row_json())["contacts"][key]["consecutive_minutes"] == 99
    popped = state["contacts"].pop(key)
    popped["consecutive_minutes"] = 100
    assert key not in json.loads(_mirror(world).row_json())["contacts"]
    assert _mirror(world).verify()


def test_a_restored_namespace_gets_the_order_of_the_python_rule(generated_scenario: Any) -> None:
    """Keys sorted by the checkpoint's JSON, which a new dict of the rule does not have."""
    world, controller, view, deltas, rng = _small(generated_scenario)
    _run_kernel(world, controller, view, deltas, rng)
    moved = replace(view, minute_index=1)
    shifted = tuple(replace(delta, minute_index=1) for delta in deltas)
    world.model_state[KEY] = json.loads(json.dumps(world.model_state[KEY], sort_keys=True))
    assert next(iter(next(iter(world.model_state[KEY]["contacts"].values())))) == (
        "consecutive_minutes"
    )
    old_record = next(iter(world.model_state[KEY]["contacts"].values()))
    python = _python_result(world, controller, moved, shifted, rng)
    _run_kernel(world, controller, moved, shifted, rng)
    assert json.dumps(world.model_state[KEY]) == json.dumps(python.runtime_state)
    assert _mirror(world).stats()["live_nodes"] == 3 + len(python.runtime_state["contacts"])
    old_record["consecutive_minutes"] = 5  # Out of the tree, so it reaches nothing.
    assert _mirror(world).verify()


def test_the_group_comes_from_the_view_and_not_the_profile(generated_scenario: Any) -> None:
    """A household in population_presence_v1 replaces profile.group_id in the view."""
    world, controller, view, deltas, rng = _small(generated_scenario)
    agents = {
        agent_id: replace(agent, group_id="everyone" if index % 2 else None)
        for index, (agent_id, agent) in enumerate(view.agents.items())
    }
    other = WorldView(minute_index=0, agents=agents, facilities={}, state_hash=None)
    python = _python_result(world, controller, other, deltas, rng)
    assert (
        python.runtime_state["contacts"]
        != _python_result(world, controller, view, deltas, rng).runtime_state["contacts"]
    )
    _run_kernel(world, controller, other, deltas, rng)
    assert json.dumps(world.model_state[KEY]) == json.dumps(python.runtime_state)


def test_a_disabled_model_returns_the_deltas_and_writes_nothing(generated_scenario: Any) -> None:
    world, controller, view, deltas, rng = _small(generated_scenario, enabled=False)
    before = _mirror(world).row_json()
    assert _run_kernel(world, controller, view, deltas, rng) is deltas
    assert _mirror(world).row_json() == before


class _OtherRNG(CounterRNG):
    pass


class _OtherPolicy(ExpressivenessChannelOpennessPolicy):
    pass


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            lambda w, c, v, d, r: {
                "controller": SimpleNamespace(
                    **{**vars(c), "artifacts": SimpleNamespace(trace_level=TraceLevel.SUMMARY)}
                )
            },
            "trace",
        ),
        (lambda w, c, v, d, r: {"rng": _OtherRNG(1)}, "RNG"),
        (
            lambda w, c, v, d, r: {
                "controller": SimpleNamespace(
                    **{**vars(c), "stress_policy": _OtherPolicy(c.stress_policy.parameters)}
                )
            },
            "policy",
        ),
        (
            lambda w, c, v, d, r: {
                "controller": SimpleNamespace(
                    **{
                        **vars(c),
                        "stress_policy": ExpressivenessChannelOpennessPolicy(
                            replace(c.stress_policy.parameters, stress_spread_per_minute=0)
                        ),
                    }
                )
            },
            "not a float",
        ),
        (lambda w, c, v, d, r: {"deltas": d[:-1]}, "different people"),
        (lambda w, c, v, d, r: {"deltas": list(d)}, "tuple"),
        (
            lambda w, c, v, d, r: {"deltas": d[:-1] + (replace(d[-1], minute_index=7),)},
            "another minute",
        ),
        (lambda w, c, v, d, r: {"deltas": d[:-1] + (SimpleNamespace(**asdict(d[-1])),)}, "class"),
        (lambda w, c, v, d, r: {"view": replace(v, minute_index=7)}, "another minute"),
        (
            lambda w, c, v, d, r: {
                "controller": SimpleNamespace(
                    **{**vars(c), "zone_neighbors": {z: "ab" for z in c.zone_neighbors}}
                )
            },
            "tuple or a list",
        ),
        (
            lambda w, c, v, d, r: {
                "controller": SimpleNamespace(
                    **{**vars(c), "zone_neighbors": {z: (1,) for z in c.zone_neighbors}}
                )
            },
            "not a str",
        ),
    ],
)
def test_what_the_kernel_does_not_handle_is_unsupported_before_any_change(
    generated_scenario: Any, change: Any, reason: str
) -> None:
    world, controller, view, deltas, rng = _small(generated_scenario)
    arguments = {"controller": controller, "view": view, "deltas": deltas, "rng": rng}
    arguments |= change(world, controller, view, deltas, rng)
    before = _mirror(world).row_json()
    with pytest.raises(native.Unsupported, match=reason):
        _rules().update_social_propagation(
            arguments["controller"],
            world,
            arguments["view"],
            arguments["deltas"],
            view.minute_index,
            arguments["rng"],
        )
    assert _mirror(world).row_json() == before


@pytest.mark.parametrize(
    ("corrupt", "text"),
    [
        (lambda state: state.__setitem__("schema_version", 2), "runtime state schema"),
        (
            lambda state: state["contacts"].__setitem__(
                "x", {"last_seen_minute": -1, "consecutive_minutes": 1}
            ),
            "last_seen_minute",
        ),
        (lambda state: state["agent_cooldown_until"].__setitem__("x", -1), "cooldown"),
        (lambda state: state["agent_cooldown_until"].__setitem__("x", 1.5), "cooldown"),
    ],
)
def test_a_state_the_rule_turns_down_keeps_the_python_error(
    generated_scenario: Any, corrupt: Any, text: str
) -> None:
    world, controller, view, deltas, rng = _small(generated_scenario)
    corrupt(world.model_state[KEY])
    with pytest.raises(native.Unsupported):
        _run_kernel(world, controller, view, deltas, rng)
    with pytest.raises(ValueError, match=text):
        controller.stress_policy.update(
            view,
            deltas,
            rng,
            zone_neighbors=controller.zone_neighbors,
            runtime_state=world.model_state[KEY],
        )


def test_a_tainted_entity_store_goes_to_python(generated_scenario: Any) -> None:
    world, controller, view, deltas, rng = _small(generated_scenario)
    next(iter(world.agents.values())).stress = 1
    with pytest.raises(native.Unsupported, match="tainted"):
        _run_kernel(world, controller, view, deltas, rng)


def test_contacts_without_weight_keep_the_python_error(generated_scenario: Any) -> None:
    world, controller, view, deltas, rng = _small(
        generated_scenario, same_zone_channel_strength=0.0, family_channel_strength=0.0
    )
    with pytest.raises(ValueError):
        _python_result(world, controller, view, deltas, rng)
    with pytest.raises(native.Unsupported, match="no weight"):
        _run_kernel(world, controller, view, deltas, rng)


def test_unsorted_deltas_are_sorted_except_for_degroot(generated_scenario: Any) -> None:
    world, controller, view, deltas, rng = _small(generated_scenario)
    backwards = tuple(reversed(deltas))
    python = _python_result(world, controller, view, backwards, rng)
    final = _run_kernel(world, controller, view, backwards, rng)
    assert [asdict(delta) for delta in final] == [asdict(delta) for delta in python.deltas]
    with pytest.raises(native.Unsupported, match="one person"):
        _run_kernel(world, controller, view, (deltas[0], *deltas[:-1]), rng)
    world, controller, view, deltas, rng = _small(generated_scenario, policy="degroot")
    with pytest.raises(native.Unsupported, match="id order"):
        _run_kernel(world, controller, view, tuple(reversed(deltas)), rng)


# ---- The step on a whole Run -------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy",
    ["sparse-family-zone-contact-v1", "degroot-contact-v1", "expressiveness-channel-openness-v1"],
)
def test_every_tick_of_a_run_matches_the_python_rule_and_writes_no_whole_namespace(
    generated_scenario_with: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    scenario = generated_scenario_with(social_policy_id=policy)
    document = scenario.model_dump(mode="json")
    document["duration_minutes"] = 90
    scenario = type(scenario).model_validate(document)
    kernel = _rules().update_social_propagation
    python_step = UpdatePhase._social_propagation
    ran = []
    refused = []
    worlds = []

    def step(self: Any, world: Any, view: Any, deltas: Any, minute: int, rng: Any) -> Any:
        assert self.stress_policy.policy_version == policy
        worlds[:] = [world]
        try:
            expected = None
            if KEY in world.model_state:
                expected = _python_result(world, self, view, deltas, rng)
            final = kernel(self, world, view, deltas, minute, rng)
        except native.Unsupported as reason:
            refused.append(str(reason))
            return python_step(self, world, view, deltas, minute, rng)
        ran.append(len(world.model_state[KEY]["contacts"]))
        assert json.dumps(world.model_state[KEY]) == json.dumps(expected.runtime_state)
        assert [asdict(delta) for delta in final] == [asdict(delta) for delta in expected.deltas]
        assert _mirror(world).verify()
        return final

    monkeypatch.setattr(OfflineSimulationController, "_social_propagation", step)
    run_scenario(scenario, tmp_path, trace_level=TraceLevel.OFF)
    assert refused == ["the namespace has no row"]
    assert len(ran) == 89 and sum(ran) > 0
    assert _mirror(worlds[0]).stats()["whole_writes"] == 1


def test_a_run_with_a_trace_is_the_same_world_as_one_without(
    generated_scenario: Any, tmp_path: Any
) -> None:
    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 30
    scenario = type(generated_scenario).model_validate(document)
    traced = run_scenario(scenario, tmp_path / "traced", trace_level=TraceLevel.SUMMARY)
    plain = run_scenario(scenario, tmp_path / "plain", trace_level=TraceLevel.OFF)
    assert traced.canonical_json() == plain.canonical_json()
    assert _mirror(traced).stats()["whole_writes"] == 30
    assert _mirror(plain).stats()["whole_writes"] == 1


# ---- The bits, on values no scenario reaches -----------------------------------------------------


class _KeepingRNG(CounterRNG):
    def __init__(self, seed: int) -> None:
        super().__init__(seed)
        self.draws: list[tuple[str, str]] = []

    def random(self, *key_parts: Any) -> float:
        value = super().random(*key_parts)
        key = json.dumps(list(key_parts), ensure_ascii=False, separators=(",", ":"))
        self.draws.append((key, value.hex()))
        return value


def _plan(
    policy: str, seed: int, view: Any, deltas: Any, settings: Any, neighbors: Any, state: Any
) -> Any:
    ids = sorted(view.agents)
    base = {delta.agent_id: delta.stress for delta in deltas}
    return _rules().plan_of_values(
        policy,
        str(seed),
        view.minute_index,
        [
            settings.max_contacts_per_agent,
            settings.minimum_contact_minutes,
            settings.cooldown_minutes,
        ],
        [
            settings.stress_spread_per_minute,
            settings.natural_recovery_per_minute,
            settings.max_pair_stress_increase,
            settings.max_agent_stress_increase_per_minute,
            settings.family_channel_strength,
            settings.same_zone_channel_strength,
            settings.nearby_zone_channel_strength,
        ],
        ids,
        [
            (
                view.agents[agent_id].group_id,
                view.agents[agent_id].zone_id,
                view.agents[agent_id].stress,
                view.agents[agent_id].stress_sensitivity,
                settings.source_expressiveness_by_agent.get(agent_id, 1.0),
                base[agent_id],
            )
            for agent_id in ids
        ],
        {zone: list(names) for zone, names in neighbors.items()},
        state["contacts"],
        state["agent_cooldown_until"],
    )


def _random_case(draw: random.Random, template: Any, policy: str) -> tuple[Any, ...]:
    names = ["A1", "A2", 'A"3', "A\\4", "é5", "ß6", "A\n7", "B8", "B9", "C10", "C11", "日12"]
    zones = ["z1", "z2", "zé", 'z"4']
    stresses = [0.0, 1.0, 0.5, 0.25, 1e-300, 1.0 - 2**-53]

    def unit() -> float:
        return draw.choice(stresses) if draw.random() < 0.3 else draw.random()

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
        )
        for agent_id in ids
    }
    view = WorldView(minute_index=minute, agents=agents, facilities={}, state_hash=None)
    neighbors = {
        zone: tuple(draw.sample(zones, draw.randrange(0, len(zones) + 1)))
        for zone in zones
        if draw.random() < 0.8
    }
    contacts = {
        json.dumps([a, b], ensure_ascii=False, separators=(",", ":")): {
            "last_seen_minute": draw.choice((minute - 1 if minute else 0, minute, 0)),
            "consecutive_minutes": draw.randrange(1, 12),
        }
        for a in names
        for b in names
        if a != b and draw.random() < 0.3
    }
    state = {
        "schema_version": 1,
        "contacts": contacts,
        "agent_cooldown_until": {
            name: draw.randrange(0, 60) for name in names if draw.random() < 0.4
        },
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
        for agent_id in sorted(ids)
    )
    settings = SocialPropagationParameters(
        enabled=True,
        max_contacts_per_agent=draw.randrange(0, 5),
        minimum_contact_minutes=draw.randrange(1, 6),
        stress_spread_per_minute=draw.choice((0.0, 0.25, draw.random() / 4)),
        natural_recovery_per_minute=draw.choice((0.0, 0.25, draw.random() / 4)),
        max_pair_stress_increase=draw.choice((0.0, 0.05, 1e-6, draw.random() / 4)),
        max_agent_stress_increase_per_minute=draw.choice((0.0, 0.05, draw.random() / 4)),
        cooldown_minutes=draw.randrange(1, 40),
        source_expressiveness_by_agent={
            name: draw.choice((0.0, 1.0, draw.random())) for name in names if draw.random() < 0.5
        },
        family_channel_strength=draw.choice((0.0, 1.0, draw.random())),
        same_zone_channel_strength=draw.choice((0.0, 1.0, draw.random())),
        nearby_zone_channel_strength=draw.choice((0.0, 1.0, draw.random())),
        source_id=POLICIES[policy][1],
    )
    return view, deltas, settings, neighbors, state


def _template(generated_scenario: Any) -> Any:
    world = WorldState.from_scenario(generated_scenario)
    return next(iter(WorldView.from_world(world, include_state_hash=False).agents.values()))


@pytest.mark.parametrize("policy", POLICIES)
def test_the_rule_matches_python_on_random_inputs(generated_scenario: Any, policy: str) -> None:
    """IDs that JSON escapes, a zone next to itself, empty channels, zero sensitivity, exact caps,
    and every cooldown and contact history a Run may not reach by itself. Each draw too."""
    draw = random.Random(2615)
    template = _template(generated_scenario)
    matched = raised = stress_moved = draws = 0
    for trial in range(400):
        view, deltas, settings, neighbors, state = _random_case(draw, template, policy)
        seed = draw.randrange(0, 2**70)
        rng = _KeepingRNG(seed)
        try:
            python = POLICIES[policy][0](settings).update(
                view, deltas, rng, zone_neighbors=neighbors, runtime_state=state
            )
        except ValueError:
            with pytest.raises(native.Unsupported):
                _plan(policy, seed, view, deltas, settings, neighbors, state)
            raised += 1
            continue
        _rules().start_draw_log()
        try:
            stresses, contacts, cooldowns, pairs = _plan(
                policy, seed, view, deltas, settings, neighbors, state
            )
        finally:
            rust_draws = _rules().take_draw_log()
        assert [(key, value.hex()) for key, value in rust_draws] == rng.draws, trial
        draws += len(rust_draws)
        by_id = {delta.agent_id: delta.stress for delta in python.deltas}
        ids = sorted(view.agents)
        assert [value.hex() for value in stresses] == [by_id[i].hex() for i in ids], trial
        assert {
            key: {"last_seen_minute": last, "consecutive_minutes": count}
            for key, last, count in contacts
        } == python.runtime_state["contacts"], trial
        assert [key for key, _, _ in contacts] == list(python.runtime_state["contacts"]), trial
        assert dict(cooldowns) == python.runtime_state["agent_cooldown_until"], trial
        assert [ids[a] for a, _ in pairs] == [item.recipient_id for item in python.contacts], trial
        assert [ids[b] for _, b in pairs] == [item.source_id for item in python.contacts], trial
        stress_moved += any(delta.stress != by_id[delta.agent_id] for delta in deltas)
        matched += 1
    assert matched > 100 and stress_moved > 5 and draws > 1000
    assert raised > 0 or policy != "channel"


@pytest.mark.parametrize("policy", POLICIES)
def test_the_comparison_sees_a_change_of_one_percent(generated_scenario: Any, policy: str) -> None:
    draw = random.Random(2615)
    template = _template(generated_scenario)
    seen = 0
    for _ in range(400):
        view, deltas, settings, neighbors, state = _random_case(draw, template, policy)
        try:
            right = _plan(policy, 1, view, deltas, settings, neighbors, state)
        except native.Unsupported:
            continue
        spread = settings.stress_spread_per_minute * 0.99
        off = replace(settings, stress_spread_per_minute=spread)
        wrong = _plan(policy, 1, view, deltas, off, neighbors, state)
        seen += right[0] != wrong[0]
    assert seen > 0
