"""body_state_v1 owned by Rust, and record_social_event in native/src/rules/body_state.rs.

domain/body_state.py, policies/affect.py and policies/relationships.py stay as the comparison base
(docs/rust-port/lanes/m3-body-state.md). The bits of the whole rule are compared on the gate by
benchmarks/rule_shadow; here, on small worlds and on values no scenario reaches.
"""

from __future__ import annotations

import copy
import json
import random
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.state_mirror import rust_accepts
from sheltersim.core.world import WorldState
from sheltersim.domain import body_state
from sheltersim.domain.body_state import (
    BODY_STATE_KEY,
    MAX_SOCIAL_HISTORY,
    MAX_SOCIAL_RELATIONSHIPS,
    create_body_state,
    validate_body_state,
)
from sheltersim.domain.models import ordered_pair_key
from sheltersim.events.models import EventKind, ScheduledEvent

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

PYTHON_RULE = body_state.record_social_event.__wrapped__ if native.available() else None


def _kernel() -> Any:
    return native.rules("body_state").record_social_event


def _rows_valid() -> Any:
    return native.state("body_state").rows_valid


def _world(
    scenario: Any, *, affect: str | None = None, relationship: str | None = None
) -> WorldState:
    world = WorldState.from_scenario(scenario)
    world.model_state[BODY_STATE_KEY] = create_body_state(world)
    selection = {}
    if affect is not None:
        selection["affect"] = affect
    if relationship is not None:
        selection["relationship"] = relationship
    if selection:
        world.model_configs = {
            **world.model_configs,
            "policy_registry": {
                "schema_version": 2,
                "psychology": "mean-reverting-life-state-v1",
                **selection,
            },
        }
    return world


def _event(kind: str, payload: dict[str, Any], number: int = 1) -> ScheduledEvent:
    return ScheduledEvent(
        event_id=f"event-{number}",
        kind=EventKind.AUTONOMOUS if hasattr(EventKind, "AUTONOMOUS") else next(iter(EventKind)),
        due_minute=10,
        priority=0,
        sequence=number,
        event_type=kind,
        payload=payload,
        causation_id=None if number % 2 else "cause",
    )


def _standin(world: WorldState) -> SimpleNamespace:
    """A world for the Python rule, holding plain copies."""
    table = {
        key: json.loads(json.dumps(world.model_state[key]))
        for key in (BODY_STATE_KEY, "movement_v1", "information_spread_v1")
        if key in world.model_state
    }
    return SimpleNamespace(
        model_state=table, agents=world.agents, model_configs=world.model_configs
    )


def _same_everywhere(world: WorldState, standin: SimpleNamespace) -> None:
    """The tree has the bits and the keys in order of the Python rule's, and the row the tree's."""
    tree = world.model_state[BODY_STATE_KEY]
    assert json.dumps(tree) == json.dumps(standin.model_state[BODY_STATE_KEY])
    assert world._core.mirror(BODY_STATE_KEY).verify()


def _both(world: WorldState, event: ScheduledEvent, minute: int = 10) -> SimpleNamespace:
    standin = _standin(world)
    PYTHON_RULE(standin, event, minute)
    _kernel()(world, event, minute)
    _same_everywhere(world, standin)
    return standin


def _pair(world: WorldState) -> tuple[str, str]:
    first, second = sorted(world.agents)[:2]
    return first, second


def _payloads(first: str, second: str) -> list[tuple[str, dict[str, Any]]]:
    interaction = {"agent_id": first, "participant_agent_id": second}
    return [
        (
            "proximity_contact_update",
            {
                "first_agent_id": first,
                "second_agent_id": second,
                "observed_minute": 9,
                "distance_m": 1.5,
                "contact_minutes": 3,
            },
        ),
        (
            "proximity_contact_update",
            {"first_agent_id": first, "second_agent_id": second, "observed_minute": 12},
        ),
        (
            "information_contact_update",
            {
                "sender_agent_id": first,
                "recipient_agent_id": second,
                "message_id": "M1",
                "contact_minutes": 2.5,
            },
        ),
        ("information_contact_update", {"sender_agent_id": second, "recipient_agent_id": first}),
        (
            "social_interaction",
            {
                **interaction,
                "interaction_type": "cooperation",
                "topic_id": "T1",
                "interaction_duration_minutes": 7,
            },
        ),
        *[
            (
                "social_interaction",
                {
                    **interaction,
                    "interaction_type": kind,
                    "topic_id": "T2",
                    "interaction_duration_minutes": 0,
                },
            )
            for kind in ("reassurance_exchange", "family_care", "voluntary_support")
        ],
        (
            "social_interaction",
            {"agent_id": first, "interaction_type": "complaint_response", "request_id": "R"},
        ),
        ("social_interaction", {**interaction, "interaction_type": "something_else"}),
        ("social_interaction", {"agent_id": "nobody", "interaction_type": "complaint_response"}),
        ("information_contact_update", {"sender_agent_id": first, "recipient_agent_id": first}),
        ("information_contact_update", {"sender_agent_id": first, "recipient_agent_id": "nobody"}),
        ("proximity_contact_update", {"first_agent_id": first, "second_agent_id": second}),
    ]


# ---- The row -------------------------------------------------------------------------------------


def test_the_row_writes_the_bytes_of_the_tree(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    for number, (kind, payload) in enumerate(_payloads(first, second)):
        _kernel()(world, _event(kind, payload, number), 10 + number)
    mirror = world._core.mirror(BODY_STATE_KEY)
    assert mirror.has_row
    assert mirror.row_json() == native.canonical_json(world.model_state[BODY_STATE_KEY])
    assert world.model_state[BODY_STATE_KEY]["history"]


@pytest.mark.parametrize(
    ("place", "value"),
    [
        (("agents", "*", "reassurance"), 1),
        (("agents", "*", "reassurance"), "x"),
        (("relationships", "k", "trust"), 1),
        (("relationships", "k", "contact_count"), 1.0),
        (("relationships", "k", "contact_count"), True),
        (("relationships", "k", "last_interaction"), 3),
        (("relationships", "k", "topic_ids"), ("a",)),
        (("schema_version",), 1.0),
    ],
)
def test_a_value_of_another_type_taints(generated_scenario: Any, place: Any, value: Any) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    _kernel()(
        world,
        _event(
            "social_interaction",
            {
                "agent_id": first,
                "participant_agent_id": second,
                "interaction_type": "voluntary_support",
            },
        ),
        10,
    )
    node: Any = world.model_state[BODY_STATE_KEY]
    for part in place[:-1]:
        if part == "*":
            part = first
        elif part == "k":
            part = ordered_pair_key(second, first)
        node = node[part]
    node[place[-1]] = value
    mirror = world._core.mirror(BODY_STATE_KEY)
    assert mirror.tainted is not None
    with pytest.raises(native.Unsupported):
        _rows_valid()(world)
    with pytest.raises(native.Unsupported):
        _kernel()(
            world,
            _event(
                "information_contact_update",
                {
                    "sender_agent_id": first,
                    "recipient_agent_id": second,
                },
            ),
            10,
        )


def test_a_write_into_an_old_history_item_taints(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    _kernel()(
        world,
        _event(
            "information_contact_update",
            {
                "sender_agent_id": first,
                "recipient_agent_id": second,
            },
        ),
        10,
    )
    world.model_state[BODY_STATE_KEY]["history"][0]["minute"] = 1
    assert world._core.mirror(BODY_STATE_KEY).tainted is not None


def test_python_writes_reach_the_row(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    state = world.model_state[BODY_STATE_KEY]
    state["agents"][first]["hygiene_need"] = 0.25
    state["relationships"][ordered_pair_key(first, second)] = {
        "contact_count": 1,
        "trust": 0.25,
        "contact_minutes": 2,
        "last_contact_minute": 3,
        "last_interaction": "x",
        "topic_ids": ["t"],
    }
    state["relationships"][ordered_pair_key(first, second)]["topic_ids"].append("u")
    state["history"].append(
        {
            "event_id": "e",
            "minute": 0,
            "kind": "k",
            "actor_ids": [first],
            "topic_id": None,
            "causation_id": None,
            "deltas": {},
        }
    )
    del state["history"][:1]
    state["relationships"].pop(ordered_pair_key(first, second))
    state["agents"].pop(second)
    mirror = world._core.mirror(BODY_STATE_KEY)
    assert mirror.has_row and mirror.verify()


def test_no_whole_write_in_ticks(generated_scenario: Any, tmp_path: Any) -> None:
    from sheltersim.experiments.runner import run_scenario
    from sheltersim.experiments.scenario import ScenarioSpec

    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 240
    world = run_scenario(ScenarioSpec.model_validate(document), tmp_path)
    mirror = world._core.mirror(BODY_STATE_KEY)
    stats = mirror.stats()
    assert mirror.has_row, mirror.tainted
    assert stats["whole_writes"] == 1
    # A handful of records and items from the Python handlers, not 700 values per tick.
    assert stats["forwards"] < 240 * 20
    assert mirror.verify()
    assert world.model_state[BODY_STATE_KEY]["relationships"]


# ---- The rule against its Python base ------------------------------------------------------


@pytest.mark.parametrize(
    "affect", ["linear-positive-negative-v1", "trust-weighted-positive-negative-v1"]
)
@pytest.mark.parametrize(
    "relationship", ["distance-duration-repeat-trust-v1", "duration-contact-trust-v1"]
)
def test_each_event_is_python_s_bit_for_bit(
    generated_scenario: Any, affect: str, relationship: str
) -> None:
    world = _world(generated_scenario, affect=affect, relationship=relationship)
    first, second = _pair(world)
    rng = random.Random(4)
    records = world.model_state[BODY_STATE_KEY]["agents"]
    for name in ("positive_affect", "negative_affect", "reassurance", "uncertainty", "discontent"):
        records[first][name] = rng.random()
        records[second][name] = rng.random()
    for number, (kind, payload) in enumerate(_payloads(first, second) * 3):
        _both(world, _event(kind, payload, number), 10 + 7 * number)


def test_many_random_events_are_python_s_bit_for_bit(generated_scenario: Any) -> None:
    world = _world(generated_scenario, affect="trust-weighted-positive-negative-v1")
    people = sorted(world.agents)[:8]
    rng = random.Random(11)
    records = world.model_state[BODY_STATE_KEY]["agents"]
    for person in people:
        for name in records[person]:
            records[person][name] = rng.random()
    choices = [
        (kind, payload) for _ in range(12) for kind, payload in _payloads(*rng.sample(people, 2))
    ]
    rng.shuffle(choices)
    for number, (kind, payload) in enumerate(choices):
        changed = dict(payload)
        if "distance_m" in changed:
            changed["distance_m"] = rng.choice([0.0, rng.random() * 20, 1e308])
        if "contact_minutes" in changed:
            changed["contact_minutes"] = rng.choice([0, 1, 2.5, 1e9, -3])
        if "interaction_duration_minutes" in changed:
            changed["interaction_duration_minutes"] = rng.choice([0, 1, 90, 0.25, -1, None, "x"])
        _both(world, _event(kind, changed, number), 10 + number)


def test_a_positions_distance_reaches_trust_and_the_deltas(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    if "movement_v1" not in world.model_state:
        pytest.skip("the scenario has no movement")
    positions = world.model_state["movement_v1"]["positions"]
    positions[first]["point"] = {"x": 1.25, "y": 2.0}
    positions[second]["point"] = {"x": 4.0, "y": 6.5}
    standin = _both(
        world,
        _event(
            "information_contact_update", {"sender_agent_id": first, "recipient_agent_id": second}
        ),
    )
    item = standin.model_state[BODY_STATE_KEY]["history"][-1]
    assert item["deltas"]["contact_distance_m"] > 0


@pytest.mark.parametrize("evidence_aware", [False, True])
def test_a_correction_is_python_s_bit_for_bit(
    generated_scenario: Any, evidence_aware: bool
) -> None:
    world = _world(generated_scenario, relationship="duration-contact-trust-v1")
    selection = world.model_configs["policy_registry"]
    if evidence_aware:
        selection["information"] = "evidence-aware-rumor-v1"
    zone = next(iter(world.agents.values())).zone_id
    records = world.model_state[BODY_STATE_KEY]["agents"]
    for record in records.values():
        record["uncertainty"] = 0.5
    people = sorted(id_ for id_, agent in world.agents.items() if agent.zone_id == zone)
    event = _event("information_correction", {"zone_id": zone, "message_id": "M"})
    # Python reads the beliefs through the same dict: the first took it, the second did not.
    beliefs = {
        people[0]: {"M": {"last_event_id": event.event_id, "last_correction_accepted": True}},
        people[-1]: {"M": {"last_event_id": "other", "last_correction_accepted": True}},
    }
    dict.__setitem__(world.model_state, "information_spread_v1", {"beliefs": beliefs})
    _both(world, event)
    items = world.model_state[BODY_STATE_KEY]["history"]
    assert [item["actor_ids"] for item in items] == (
        [[people[0]]] if evidence_aware else [[person] for person in people]
    )


def test_the_history_keeps_its_limit_in_the_row_and_the_tree(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    state = world.model_state[BODY_STATE_KEY]
    item = {
        "event_id": "old",
        "minute": 0,
        "kind": "k",
        "actor_ids": [first],
        "topic_id": None,
        "causation_id": None,
        "deltas": {},
    }
    state["history"].extend(
        [dict(item, event_id=f"old-{n}") for n in range(MAX_SOCIAL_HISTORY - 1)]
    )
    live = world._core.mirror(BODY_STATE_KEY).stats()["live_nodes"]
    event = _event(
        "social_interaction",
        {
            "agent_id": first,
            "participant_agent_id": second,
            "interaction_type": "voluntary_support",
        },
    )
    _kernel()(world, event, 10)
    history = state["history"]
    assert len(history) == MAX_SOCIAL_HISTORY
    assert history[0]["event_id"] == "old-1"
    assert world._core.mirror(BODY_STATE_KEY).verify()
    # Every dropped item left the table: nothing grows at the limit.
    after = world._core.mirror(BODY_STATE_KEY).stats()["live_nodes"]
    for number in range(5):
        _kernel()(world, _event("social_interaction", dict(event.payload), number + 2), 11)
    assert world._core.mirror(BODY_STATE_KEY).stats()["live_nodes"] == after
    assert after >= live
    assert world._core.mirror(BODY_STATE_KEY).verify()


def test_the_relationship_limit_stops_new_pairs_only(
    generated_scenario: Any, monkeypatch: Any
) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    state = world.model_state[BODY_STATE_KEY]
    # Filled on a plain copy and loaded whole: 100,000 single writes would only take long.
    plain = json.loads(json.dumps(state))
    other_a, other_b = sorted(world.agents)[2:4]
    record = {
        "contact_count": 0,
        "trust": 0.5,
        "contact_minutes": 0,
        "last_contact_minute": None,
        "last_interaction": None,
        "topic_ids": [],
    }
    for number in range(MAX_SOCIAL_RELATIONSHIPS):
        plain["relationships"][f'["{other_a}","{other_b}-{number}"]'] = dict(record)
    world.model_state[BODY_STATE_KEY] = plain
    event = _event(
        "information_contact_update", {"sender_agent_id": first, "recipient_agent_id": second}
    )
    _kernel()(world, event, 10)
    state = world.model_state[BODY_STATE_KEY]
    assert ordered_pair_key(second, first) not in state["relationships"]
    # The affect moved all the same, and its item is there, without a relationship update.
    assert [item["kind"] for item in state["history"]] == ["information_contact"]
    assert world._core.mirror(BODY_STATE_KEY).verify()


# ---- social_network_v1 reads this namespace through the tree ---------------------------------


def _with_social(scenario: Any) -> WorldState:
    from sheltersim.domain.social_network import (
        SOCIAL_NETWORK_STATE_KEY,
        create_social_network_state,
    )

    world = _world(scenario)
    if SOCIAL_NETWORK_STATE_KEY not in world.model_state:
        world.model_state[SOCIAL_NETWORK_STATE_KEY] = create_social_network_state()
    return world


@pytest.mark.parametrize(
    "interaction", ["cooperation", "reassurance_exchange", "family_care", "voluntary_support"]
)
def test_observe_event_copies_the_trust_this_kernel_wrote(
    generated_scenario: Any, interaction: str
) -> None:
    """handlers.py calls record_social_event, then observe_event, which reads the tree."""
    from sheltersim.domain.social_network import SOCIAL_NETWORK_STATE_KEY, observe_event

    world = _with_social(generated_scenario)
    first, second = _pair(world)
    python = copy.deepcopy(world)
    observe = native.rules("social_network").observe_event
    for number in range(4):
        event = _event(
            "social_interaction",
            {
                "agent_id": first,
                "participant_agent_id": second,
                "interaction_type": interaction,
                "topic_id": "T1",
                "interaction_duration_minutes": 5,
            },
            number + 1,
        )
        minute = 10 + 30 * number
        _kernel()(world, event, minute)
        observe(world, event, minute)
        PYTHON_RULE(python, event, minute)
        observe_event.__wrapped__(python, event, minute)
    for name in (BODY_STATE_KEY, SOCIAL_NETWORK_STATE_KEY):
        assert json.dumps(world.model_state[name]) == json.dumps(python.model_state[name])
        mirror = world._core.mirror(name)
        assert mirror.has_row and mirror.verify(), mirror.tainted
    body = world.model_state[BODY_STATE_KEY]["relationships"]
    social = world.model_state[SOCIAL_NETWORK_STATE_KEY]["relationships"]
    key = ordered_pair_key(second, first)
    assert body[key]["trust"] != 0.5
    assert social[key]["trust"].hex() == body[key]["trust"].hex()


def test_observe_event_sees_a_tainted_body_tree_too(generated_scenario: Any) -> None:
    """A body namespace without a row still has the tree that observe_event reads."""
    from sheltersim.domain.social_network import SOCIAL_NETWORK_STATE_KEY

    world = _with_social(generated_scenario)
    first, second = _pair(world)
    event = _event(
        "social_interaction",
        {"agent_id": first, "participant_agent_id": second, "interaction_type": "cooperation"},
    )
    _kernel()(world, event, 10)
    world.model_state[BODY_STATE_KEY]["agents"][first]["reassurance"] = 1
    assert world._core.mirror(BODY_STATE_KEY).tainted is not None
    body_state.record_social_event(world, event, 11)
    native.rules("social_network").observe_event(world, event, 11)
    key = ordered_pair_key(second, first)
    assert (
        world.model_state[SOCIAL_NETWORK_STATE_KEY]["relationships"][key]["trust"]
        == world.model_state[BODY_STATE_KEY]["relationships"][key]["trust"]
    )


# ---- What is left to Python ----------------------------------------------------------------


def _untouched(world: WorldState) -> bytes:
    return world._core.mirror(BODY_STATE_KEY).row_json()


@pytest.mark.parametrize(
    ("kind", "payload", "why"),
    [
        ("information_contact_update", {"sender_agent_id": 3, "recipient_agent_id": "x"}, "ID"),
        ("proximity_contact_update", {"distance_m": 2}, "distance"),
        ("proximity_contact_update", {"distance_m": "far"}, "distance"),
        ("social_interaction", {"agent_id": ["x"]}, "ID"),
        ("information_correction", {"zone_id": 5}, "zone"),
    ],
)
def test_what_python_decides_changes_nothing_here(
    generated_scenario: Any, kind: str, payload: dict[str, Any], why: str
) -> None:
    world = _world(generated_scenario)
    before = _untouched(world)
    with pytest.raises(native.Unsupported, match=why):
        _kernel()(world, _event(kind, payload), 10)
    assert _untouched(world) == before


def test_a_person_without_a_record_raises_python_s_error(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    world.model_state[BODY_STATE_KEY]["agents"].pop(second)
    event = _event(
        "information_contact_update", {"sender_agent_id": first, "recipient_agent_id": second}
    )
    before = _untouched(world)
    with pytest.raises(native.Unsupported, match="no body record"):
        _kernel()(world, event, 10)
    assert _untouched(world) == before
    with pytest.raises(KeyError):
        body_state.record_social_event(world, event, 10)


def test_a_relationship_without_trust_raises_python_s_error(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    state = world.model_state[BODY_STATE_KEY]
    state["relationships"][ordered_pair_key(second, first)] = {
        "contact_count": 0,
        "trust": None,
        "contact_minutes": 0,
        "last_contact_minute": None,
        "last_interaction": None,
        "topic_ids": [],
    }
    event = _event(
        "information_contact_update", {"sender_agent_id": first, "recipient_agent_id": second}
    )
    before = _untouched(world)
    with pytest.raises(native.Unsupported, match="no trust"):
        _kernel()(world, event, 10)
    assert _untouched(world) == before
    with pytest.raises(TypeError):
        body_state.record_social_event(world, event, 10)


def test_a_model_this_kernel_does_not_know_is_python_s(
    generated_scenario: Any, monkeypatch: Any
) -> None:
    from sheltersim.contracts import policies

    world = _world(generated_scenario)
    first, second = _pair(world)
    event = _event(
        "information_contact_update", {"sender_agent_id": first, "recipient_agent_id": second}
    )
    real = policies.selected_models
    monkeypatch.setattr(
        policies, "selected_models", lambda configs: {**real(configs), "affect": "new"}
    )
    with pytest.raises(native.Unsupported, match="affect"):
        _kernel()(world, event, 10)


def test_without_the_namespace_nothing_happens(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    world.model_state.pop(BODY_STATE_KEY, None)
    assert _kernel()(world, _event("information_correction", {"zone_id": "z"}), 10) is None


def test_the_frozen_payload_is_what_the_kernel_reads(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    event = _event(
        "information_contact_update", {"sender_agent_id": first, "recipient_agent_id": second}
    )
    assert isinstance(event.payload, MappingProxyType)
    _kernel()(world, event, 10)
    assert world.model_state[BODY_STATE_KEY]["history"]


# ---- Validation ----------------------------------------------------------------------------


def _record() -> dict[str, Any]:
    return {
        "contact_count": 1,
        "trust": 0.5,
        "contact_minutes": 2,
        "last_contact_minute": 0,
        "last_interaction": None,
        "topic_ids": [],
    }


def _judged(world: WorldState) -> tuple[bool, str | None]:
    """The kernel's word, and what the Python rules say with no kernel."""
    try:
        accepted = _rows_valid()(world) is True
    except native.Unsupported:
        accepted = False
    kept = body_state._ROWS_VALID
    body_state._ROWS_VALID = None
    try:
        validate_body_state(world)
        error = None
    except (ValueError, TypeError, KeyError) as raised:
        error = str(raised)
    finally:
        body_state._ROWS_VALID = kept
    return accepted, error


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"contact_count": -1},
        {"contact_count": True},
        {"contact_minutes": 2.0},
        {"contact_minutes": 10**30},
        {"topic_ids": ("a",)},
        {"topic_ids": ["a"] * 32},
        {"topic_ids": ["a"] * 33},
        {"topic_ids": ["a" * 64, "é" * 64]},
        {"topic_ids": ["a" * 64, "b" * 65]},
        {"topic_ids": ["é" * 65]},
        {"topic_ids": [1]},
        {"trust": None},
        {"trust": 1},
        {"trust": 1.0},
        {"trust": 0.0},
        {"trust": -0.1},
        {"trust": 1.0000000001},
        {"trust": float("nan")},
        {"trust": False},
        {"last_contact_minute": None},
        {"last_contact_minute": -3},
        {"last_interaction": "x"},
        {"last_interaction": 4},
        {"extra": 1},
    ],
)
def test_a_relationship_is_never_taken_when_python_refuses_it(
    generated_scenario: Any, change: Any
) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    # A written record, and one that was in the state from the start, take different paths.
    relationships = world.model_state[BODY_STATE_KEY]["relationships"]
    assert _rows_valid()(world) is True
    relationships[ordered_pair_key(first, second)] = {**_record(), **change}
    accepted, error = _judged(world)
    assert not (accepted and error), error

    loaded = _world(generated_scenario)
    plain = json.loads(json.dumps(loaded.model_state[BODY_STATE_KEY]))
    plain["relationships"][ordered_pair_key(first, second)] = {**_record(), **change}
    loaded.model_state[BODY_STATE_KEY] = plain
    accepted, error = _judged(loaded)
    assert not (accepted and error), error


@pytest.mark.parametrize(
    "key",
    [
        '["{a}","{b}"]',
        '["{a}","{a}"]',
        '["{a}","nobody"]',
        '[ "{a}","{b}"]',
        '["{a}", "{b}"]',
        '["{a}","{b}","{a}"]',
        '["{a}"]',
        "not json",
        "",
        '{{"{a}":"{b}"}}',
        '["\\u0041{a}","{b}"]',
    ],
)
def test_a_key_is_never_taken_when_python_refuses_it(generated_scenario: Any, key: str) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    assert _rows_valid()(world) is True
    world.model_state[BODY_STATE_KEY]["relationships"][key.format(a=first, b=second)] = _record()
    accepted, error = _judged(world)
    assert not (accepted and error), error
    if key == '["{a}","{b}"]':
        assert accepted and error is None
    else:
        assert not accepted


def test_a_key_of_someone_who_left_is_read_again(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    state = world.model_state[BODY_STATE_KEY]
    state["relationships"][ordered_pair_key(first, second)] = _record()
    assert _rows_valid()(world) is True
    state["agents"].pop(second)
    world.agents.pop(second)
    assert _rows_valid()(world) is False
    with pytest.raises(ValueError, match="relationship record is invalid"):
        validate_body_state(world)


@pytest.mark.parametrize(
    ("name", "value", "accepted"),
    [
        ("sleep_debt_minutes", 1440.0, True),
        ("sleep_debt_minutes", 1440.5, False),
        ("reassurance", 1.0, True),
        ("reassurance", 0.0, True),
        ("reassurance", -0.0, True),
        ("reassurance", 1.01, False),
        ("hygiene_need", -0.01, False),
        ("negative_affect", float("nan"), False),
        ("positive_affect", float("inf"), False),
    ],
)
def test_a_record_s_bounds(
    generated_scenario: Any, name: str, value: float, accepted: bool
) -> None:
    world = _world(generated_scenario)
    first, _ = _pair(world)
    world.model_state[BODY_STATE_KEY]["agents"][first][name] = value
    took, error = _judged(world)
    assert took is accepted
    assert (error is None) is accepted


def test_a_tick_s_write_of_a_kernel_is_read_by_the_next_check(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    assert _rows_valid()(world) is True
    _kernel()(
        world,
        _event(
            "social_interaction",
            {
                "agent_id": first,
                "participant_agent_id": second,
                "interaction_type": "cooperation",
            },
        ),
        10,
    )
    assert _rows_valid()(world) is True
    assert _judged(world) == (True, None)


# Each way to write the state, with the text the Python rules raise (t-tick-validation.md).
_ROUTES = {
    "a tracked write": lambda world, first: world.model_state[BODY_STATE_KEY]["agents"][
        first
    ].__setitem__("reassurance", 1.5),
    "a tracked update": lambda world, first: world.model_state[BODY_STATE_KEY]["agents"][
        first
    ].update({"reassurance": 1.5}),
    "the whole namespace": lambda world, first: world.model_state.__setitem__(
        BODY_STATE_KEY,
        {
            **json.loads(json.dumps(world.model_state[BODY_STATE_KEY])),
            "agents": {
                key: ({**value, "reassurance": 1.5} if key == first else value)
                for key, value in json.loads(
                    json.dumps(world.model_state[BODY_STATE_KEY]["agents"])
                ).items()
            },
        },
    ),
    "the whole of model_state": lambda world, first: setattr(
        world,
        "model_state",
        {
            **dict(world.model_state),
            BODY_STATE_KEY: {
                **json.loads(json.dumps(world.model_state[BODY_STATE_KEY])),
                "agents": {
                    key: ({**value, "reassurance": 1.5} if key == first else value)
                    for key, value in json.loads(
                        json.dumps(world.model_state[BODY_STATE_KEY]["agents"])
                    ).items()
                },
            },
        },
    ),
    "the world's __dict__": lambda world, first: world.__dict__.__setitem__(
        "model_state",
        {
            **dict(world.model_state),
            BODY_STATE_KEY: {
                **json.loads(json.dumps(world.model_state[BODY_STATE_KEY])),
                "agents": {
                    key: ({**value, "reassurance": 1.5} if key == first else value)
                    for key, value in json.loads(
                        json.dumps(world.model_state[BODY_STATE_KEY]["agents"])
                    ).items()
                },
            },
        },
    ),
    "past the table": lambda world, first: dict.__setitem__(
        world.model_state,
        BODY_STATE_KEY,
        {
            **json.loads(json.dumps(world.model_state[BODY_STATE_KEY])),
            "agents": {
                key: ({**value, "reassurance": 1.5} if key == first else value)
                for key, value in json.loads(
                    json.dumps(world.model_state[BODY_STATE_KEY]["agents"])
                ).items()
            },
        },
    ),
}


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_each_write_route_is_stopped_with_python_s_text(
    generated_scenario: Any, route: str
) -> None:
    """What proves it for validate_body_state with the kernel on: no route gets a yes."""
    world = _world(generated_scenario)
    first, _ = _pair(world)
    assert rust_accepts(world, BODY_STATE_KEY, _rows_valid())
    _ROUTES[route](world, first)
    assert not rust_accepts(world, BODY_STATE_KEY, _rows_valid())
    with pytest.raises(ValueError) as raised:
        validate_body_state(world)
    assert str(raised.value) == f"body_state_v1 reassurance for {first!r} is invalid"


def test_a_relationship_written_in_each_route_is_read(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    relationships = world.model_state[BODY_STATE_KEY]["relationships"]
    key = ordered_pair_key(first, second)
    relationships[key] = _record()
    assert _rows_valid()(world) is True
    relationships[key]["trust"] = 1.5
    assert _rows_valid()(world) is False
    with pytest.raises(ValueError, match="trust must be within"):
        validate_body_state(world)
    relationships[key]["trust"] = 0.5
    assert _rows_valid()(world) is True
    relationships[key]["topic_ids"].append("t" * 65)
    assert _rows_valid()(world) is False
    with pytest.raises(ValueError, match="relationship record is invalid"):
        validate_body_state(world)
    relationships[key]["topic_ids"].pop()
    assert _rows_valid()(world) is True


def test_a_no_starts_the_next_check_over(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    relationships = world.model_state[BODY_STATE_KEY]["relationships"]
    relationships[ordered_pair_key(first, second)] = _record()
    assert _rows_valid()(world) is True
    # The Python rule accepts this key; the kernel says no each time, and it does not pile up.
    relationships['["é","a"]'] = _record()
    for _ in range(3):
        assert _rows_valid()(world) is False
    relationships.pop('["é","a"]')
    assert _rows_valid()(world) is True


def test_a_write_past_a_tracked_dict_is_seen_only_by_the_python_rules(
    generated_scenario: Any,
) -> None:
    """As for the namespaces of lane T: src/ does not do this, the lint and verify() find it."""
    world = _world(generated_scenario)
    first, _ = _pair(world)
    dict.__setitem__(world.model_state[BODY_STATE_KEY]["agents"][first], "reassurance", 1.5)
    assert _rows_valid()(world) is True
    assert not world._core.mirror(BODY_STATE_KEY).verify()


def test_a_copy_of_the_world_owns_a_row_of_its_own(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    first, second = _pair(world)
    copied = copy.deepcopy(world)
    _kernel()(
        copied,
        _event(
            "information_contact_update",
            {
                "sender_agent_id": first,
                "recipient_agent_id": second,
            },
        ),
        10,
    )
    assert world.model_state[BODY_STATE_KEY]["history"] == []
    assert world._core.mirror(BODY_STATE_KEY).verify()
    assert copied._core.mirror(BODY_STATE_KEY).verify()
    assert copied.model_state[BODY_STATE_KEY]["history"]
